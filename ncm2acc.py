#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ncm2acc — 监听文件夹中的新 .ncm 文件并自动处理：

    1) 用 ncmdump-go.exe 把 .ncm 解密成 mp3 / flac（完整歌曲）
    2) 用 audio-separator (BS-Roformer, GPU) 去人声，提取伴奏(instrumental)

处理完成后保留：伴奏 + 解密后的完整歌曲（都放在 output\\）。

去重方式（config.toml 的 dedup 或 --dedup）：
    - record（默认）：原 .ncm 留在原地不动，用内容哈希清单去重
                      （清单文件 watch\\.ncm2acc_processed.json）
    - move          ：处理后把原 .ncm 移到 watch\\processed\\，失败移到 watch\\failed\\

用法：
    python ncm2acc.py                # 持续监听 .\watch 文件夹（默认）
    python ncm2acc.py --watch D:\Music\ncm
    python ncm2acc.py --once         # 处理当前所有 .ncm 后退出（批处理）
"""

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

# ----------------------------- 默认配置 -----------------------------
HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "watch":     HERE / "watch",                                  # 监听目录（把 .ncm 丢进来）
    "output":    HERE / "output",                                 # 伴奏 + 完整歌曲输出目录
    "ncmdump":   HERE / "ncmdump-go.exe",                         # 解密程序
    "model":     "model_bs_roformer_ep_317_sdr_12.9755.ckpt",     # BS-Roformer（音质最强）
    "model_dir": HERE / "models",                                 # 模型缓存目录（首次自动下载）
    "fmt":       "MP3",                                           # 伴奏输出格式
    "bitrate":   "320k",                                          # 伴奏 mp3 码率
    "poll":      3.0,                                             # 轮询间隔（秒）
    "stable":    2,                                               # 文件大小连续 N 次不变才视为写入完成
    "dedup":     "record",                                        # 去重方式：record（哈希清单）/ move（移动文件）
}

# ncmdump 可能输出的音频后缀（.ncm 内部可能是 mp3 或 flac）
AUDIO_EXTS = {".mp3", ".flac", ".wav", ".m4a", ".ogg", ".aac"}

log = logging.getLogger("ncm2acc")


# ----------------------------- 工具函数 -----------------------------
def setup_logging(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    log.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    fh = logging.FileHandler(output_dir / "ncm2acc.log", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)


def snapshot(d: Path) -> dict:
    """目录内 文件名 -> 修改时间 的快照，用于检测 ncmdump 新产生/覆盖的文件。"""
    out = {}
    if d.exists():
        for p in d.iterdir():
            if p.is_file():
                try:
                    out[p.name] = p.stat().st_mtime
                except OSError:
                    pass
    return out


def decrypt_ncm(ncm: Path, out_dir: Path, ncmdump: Path) -> Path | None:
    """调用 ncmdump-go.exe 解密。返回解密出的歌曲路径，失败返回 None。

    注意：ncmdump-go 即使失败也返回 exitcode 0，所以这里靠对比输出目录
    （新增 / 被覆盖的音频文件）来判断是否真的成功。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    before = snapshot(out_dir)
    try:
        subprocess.run(
            [str(ncmdump), str(ncm), "-o", str(out_dir)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except Exception as e:  # noqa: BLE001
        log.error("调用 ncmdump-go 出错：%s", e)
        return None

    after = snapshot(out_dir)
    changed = [
        out_dir / name
        for name, mtime in after.items()
        if before.get(name) != mtime and Path(name).suffix.lower() in AUDIO_EXTS
    ]
    if not changed:
        log.error("解密失败（未产生音频文件）：%s", ncm.name)
        return None
    # 取体积最大的那个就是歌曲本体
    return max(changed, key=lambda p: p.stat().st_size)


def extract_instrumental(separator, song: Path, fmt: str) -> Path | None:
    """用 audio-separator 提取伴奏，返回伴奏文件路径（已重命名为 '<歌名> (伴奏).<fmt>'）。"""
    out_dir = Path(separator.output_dir)
    results = separator.separate(str(song))  # output_single_stem=Instrumental → 只产生一个文件

    # results 里既可能是文件名也可能是绝对路径，统一成绝对路径
    produced = []
    for r in results:
        p = Path(r)
        produced.append(p if p.is_absolute() else out_dir / p)

    inst = next((p for p in produced if "instrumental" in p.name.lower()), None)
    if inst is None:
        inst = produced[0] if produced else None
    if inst is None or not inst.exists():
        log.error("人声分离失败：%s", song.name)
        return None

    clean = out_dir / f"{song.stem} (伴奏).{fmt.lower()}"
    if inst.resolve() != clean.resolve():
        if clean.exists():
            clean.unlink()
        inst.rename(clean)
    return clean


# ----------------------------- 去重策略 -----------------------------
def file_hash(path: Path, chunk: int = 1 << 20) -> str:
    """计算文件 SHA-256（按块读取，省内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


class MoveDedup:
    """move 模式：处理后把原 .ncm 移到 processed\\ / failed\\，靠“文件已移走”去重。"""

    def __init__(self, watch: Path):
        self.processed = watch / "processed"; self.processed.mkdir(parents=True, exist_ok=True)
        self.failed = watch / "failed"; self.failed.mkdir(parents=True, exist_ok=True)

    def pre_skip(self, ncm: Path, size: int, mtime: float) -> bool:
        return False

    def need_process(self, ncm: Path, size: int, mtime: float) -> bool:
        return True

    def mark(self, ncm: Path, ok: bool) -> None:
        dest = self.processed if ok else self.failed
        shutil.move(str(ncm), str(dest / ncm.name))


class RecordDedup:
    """record 模式：原 .ncm 留在原地，用内容哈希清单 .ncm2acc_processed.json 去重。"""

    def __init__(self, watch: Path):
        self.manifest = watch / ".ncm2acc_processed.json"
        self.done, self.failed = self._load()
        self._cache: dict[str, tuple] = {}   # path -> (size, mtime)：已判定，跳过且不再哈希
        self._hash: dict[str, str] = {}      # path -> hash：本轮暂存供 mark 复用

    def _load(self):
        if self.manifest.exists():
            try:
                d = json.loads(self.manifest.read_text("utf-8"))
                return d.get("done", {}), d.get("failed", {})
            except Exception:  # noqa: BLE001
                log.warning("清单文件损坏，将重建：%s", self.manifest)
        return {}, {}

    def _save(self):
        tmp = self.manifest.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"done": self.done, "failed": self.failed}, ensure_ascii=False, indent=1),
            "utf-8",
        )
        tmp.replace(self.manifest)   # 原子替换，避免写一半损坏

    def pre_skip(self, ncm: Path, size: int, mtime: float) -> bool:
        # 已判定过且大小/时间未变 → 直接跳过，连哈希都不算
        return self._cache.get(str(ncm)) == (size, mtime)

    def need_process(self, ncm: Path, size: int, mtime: float) -> bool:
        h = file_hash(ncm)
        self._hash[str(ncm)] = h
        if h in self.done or h in self.failed:
            self._cache[str(ncm)] = (size, mtime)   # 记住，避免下轮重复哈希
            return False
        return True

    def mark(self, ncm: Path, ok: bool) -> None:
        h = self._hash.pop(str(ncm), None) or file_hash(ncm)
        rec = {"name": ncm.name, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        (self.done if ok else self.failed)[h] = rec
        self._save()
        try:
            st = ncm.stat()
            self._cache[str(ncm)] = (st.st_size, st.st_mtime)
        except OSError:
            pass


def make_dedup(cfg: dict, watch: Path):
    return RecordDedup(watch) if cfg["dedup"] == "record" else MoveDedup(watch)


def process_one(ncm: Path, cfg: dict, separator) -> bool:
    """处理单个 .ncm；成功返回 True，失败返回 False（移动/记录由调用方按去重策略决定）。"""
    log.info("▶ 处理：%s", ncm.name)
    t0 = time.time()

    song = decrypt_ncm(ncm, cfg["output"], cfg["ncmdump"])
    if song is None:
        return False
    log.info("  ✓ 解密 → %s", song.name)

    inst = extract_instrumental(separator, song, cfg["fmt"])
    if inst is None:
        return False
    log.info("  ✓ 伴奏 → %s", inst.name)

    log.info("✔ 完成（%.1fs）：%s", time.time() - t0, ncm.name)
    return True


def make_separator(cfg: dict):
    """构建并加载 audio-separator（首次会自动下载模型）。"""
    try:
        from audio_separator.separator import Separator
    except ImportError:
        log.error("未安装 audio-separator。请先运行：")
        log.error('    pip install "audio-separator[gpu]"')
        sys.exit(1)

    log.info("加载分离模型：%s（首次会自动下载，请稍候…）", cfg["model"])
    sep = Separator(
        output_dir=str(cfg["output"]),
        output_format=cfg["fmt"],
        output_bitrate=cfg["bitrate"],
        output_single_stem="Instrumental",   # 只要伴奏
        model_file_dir=str(cfg["model_dir"]),
    )
    sep.load_model(model_filename=cfg["model"])
    log.info("模型就绪。")
    return sep


# ----------------------------- 运行模式 -----------------------------
def run_once(cfg: dict, separator, dedup) -> None:
    watch = cfg["watch"]
    files = sorted(watch.glob("*.ncm"))
    if not files:
        log.info("没有待处理的 .ncm 文件：%s", watch)
        return
    log.info("批处理 %d 个文件…", len(files))
    for ncm in files:
        try:
            st = ncm.stat()
            if not dedup.need_process(ncm, st.st_size, st.st_mtime):
                log.info("⏭ 已处理过，跳过：%s", ncm.name)
                continue
            ok = process_one(ncm, cfg, separator)
            dedup.mark(ncm, ok)
        except Exception as e:  # noqa: BLE001
            log.exception("处理 %s 时出错：%s", ncm.name, e)


def run_watch(cfg: dict, separator, dedup) -> None:
    watch = cfg["watch"]; watch.mkdir(parents=True, exist_ok=True)

    log.info("开始监听（每 %.0fs 轮询一次，去重=%s）：%s", cfg["poll"], cfg["dedup"], watch)
    log.info("把 .ncm 文件丢进该文件夹即可自动处理。Ctrl+C 退出。")

    seen: dict[str, tuple] = {}  # path -> (last_size, stable_count)
    while True:
        for ncm in sorted(watch.glob("*.ncm")):
            key = str(ncm)
            try:
                st = ncm.stat()
            except OSError:
                continue
            size, mtime = st.st_size, st.st_mtime
            if dedup.pre_skip(ncm, size, mtime):    # 已处理过且未变动 → 廉价跳过
                continue
            last_size, cnt = seen.get(key, (None, 0))
            cnt = cnt + 1 if (size > 0 and size == last_size) else 0
            seen[key] = (size, cnt)
            if cnt >= cfg["stable"]:          # 大小稳定，认为写入完成
                seen.pop(key, None)
                try:
                    if not dedup.need_process(ncm, size, mtime):
                        continue
                    ok = process_one(ncm, cfg, separator)
                    dedup.mark(ncm, ok)
                except Exception as e:  # noqa: BLE001
                    log.exception("处理 %s 时出错：%s", ncm.name, e)
        # 清理已不存在文件的记录
        seen = {k: v for k, v in seen.items() if Path(k).exists()}
        time.sleep(cfg["poll"])


# ----------------------------- 入口 -----------------------------
def load_config() -> dict:
    """读取 config.toml（在脚本同目录），覆盖内置默认值。文件不存在则用默认值。

    优先级：命令行参数 > config.toml > 内置 DEFAULTS。
    """
    cfg = dict(DEFAULTS)
    cfg_path = HERE / "config.toml"
    if not cfg_path.exists():
        return cfg
    try:
        with open(cfg_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 读取 config.toml 失败，改用默认值：{e}")
        return cfg
    # 路径类（空字符串视为“用默认”）
    for k in ("watch", "output", "ncmdump", "model_dir"):
        if data.get(k):
            cfg[k] = Path(data[k]).expanduser()
    # 字符串类
    for k in ("model", "fmt", "bitrate"):
        if data.get(k):
            cfg[k] = data[k]
    # 数值类
    if data.get("poll") is not None:
        cfg["poll"] = float(data["poll"])
    if data.get("stable") is not None:
        cfg["stable"] = int(data["stable"])
    # 去重方式（只接受合法值，否则保持默认）
    if data.get("dedup") in ("record", "move"):
        cfg["dedup"] = data["dedup"]
    return cfg


def parse_args() -> dict:
    base = load_config()
    ap = argparse.ArgumentParser(description="监听 .ncm → 解密 mp3 → 提取伴奏")
    ap.add_argument("--watch",   type=Path, default=base["watch"],   help="监听目录")
    ap.add_argument("--output",  type=Path, default=base["output"],  help="输出目录")
    ap.add_argument("--ncmdump", type=Path, default=base["ncmdump"], help="ncmdump-go.exe 路径")
    ap.add_argument("--model",   default=base["model"],              help="分离模型文件名")
    ap.add_argument("--fmt",     default=base["fmt"],                help="伴奏输出格式 (MP3/FLAC/WAV)")
    ap.add_argument("--bitrate", default=base["bitrate"],            help="伴奏 mp3 码率")
    ap.add_argument("--poll",    type=float, default=base["poll"],   help="轮询间隔(秒)")
    ap.add_argument("--stable",  type=int,   default=base["stable"], help="写入稳定检测次数")
    ap.add_argument("--dedup",   choices=["record", "move"], default=base["dedup"],
                    help="去重方式：record=原文件留在原地用哈希清单 / move=移到 processed")
    ap.add_argument("--once",    action="store_true",                help="批处理一次后退出")
    a = ap.parse_args()
    return {
        "watch": a.watch, "output": a.output, "ncmdump": a.ncmdump,
        "model": a.model, "model_dir": base["model_dir"],
        "fmt": a.fmt, "bitrate": a.bitrate, "poll": a.poll,
        "stable": a.stable, "dedup": a.dedup, "once": a.once,
    }


def main() -> None:
    cfg = parse_args()
    setup_logging(cfg["output"])

    if not cfg["ncmdump"].exists():
        log.error("找不到 ncmdump-go.exe：%s", cfg["ncmdump"])
        sys.exit(1)

    cfg["watch"].mkdir(parents=True, exist_ok=True)
    dedup = make_dedup(cfg, cfg["watch"])
    separator = make_separator(cfg)

    try:
        if cfg["once"]:
            run_once(cfg, separator, dedup)
        else:
            run_watch(cfg, separator, dedup)
    except KeyboardInterrupt:
        log.info("已退出。")


if __name__ == "__main__":
    main()
