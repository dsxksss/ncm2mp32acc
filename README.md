# ncm2acc — 自动 .ncm → mp3 → 伴奏

监听一个文件夹，发现新的 `.ncm` 文件就自动：

1. 用 `ncmdump-go.exe` 解密成 mp3 / flac（**完整歌曲**）
2. 用 [`audio-separator`](https://github.com/nomadkaraoke/python-audio-separator)（BS-Roformer 模型，走 GPU）去人声，提取**伴奏**

结果都放在 `output\`：
- `<歌名>.mp3`（或 `.flac`）—— 解密后的完整歌曲
- `<歌名> (伴奏).mp3` —— 提取出的伴奏

**避免重复处理**有两种方式（config.toml 的 `dedup` 或 `--dedup`）：
- `record`（默认）：原 `.ncm` **留在原地不动**，用内容哈希记到清单 `watch\.ncm2acc_processed.json` 去重。
- `move`：处理成功后把原 `.ncm` 移到 `watch\processed\`，失败的移到 `watch\failed\`。

---

## 安装（一次性）

本机已具备：Python 3.11、ffmpeg、NVIDIA RTX 4060 Ti（CUDA）。

```powershell
cd E:\ncm2mp32acc
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install "audio-separator[gpu]"
```

> 纯 CPU 机器把最后一行换成 `audio-separator[cpu]`（速度慢很多）。

---

## 使用

**持续监听（默认）**：双击 `run.bat`，或

```powershell
.\.venv\Scripts\python.exe ncm2acc.py
```

然后把 `.ncm` 文件丢进 `watch\` 文件夹即可，程序会自动处理。`Ctrl+C` 退出。

**批处理一次后退出**：

```powershell
.\.venv\Scripts\python.exe ncm2acc.py --once
```

**监听其它文件夹**：

```powershell
.\.venv\Scripts\python.exe ncm2acc.py --watch "D:\Music\网易云下载"
```

### 配置文件 config.toml（推荐）

不想每次敲参数？复制模板 [`config.toml.example`](config.toml.example) 为 `config.toml`，改监听 / 输出路径，保存即生效：

```powershell
copy config.toml.example config.toml
```

```toml
watch  = 'D:\Music\网易云下载'   # 监听目录
output = 'D:\Music\伴奏'          # 输出目录
```

> `config.toml` 已被 `.gitignore` 忽略（含本机路径，不纳入版本控制）。没有它时程序回落到内置默认（程序目录下的 `watch\` / `output\`）。

优先级：**命令行参数 > config.toml > 内置默认**。某项留空 `''` 即用默认值。

常用参数（会覆盖 config.toml）：

| 参数 | 说明 | 默认 |
|------|------|------|
| `--watch`   | 监听目录 | `.\watch` |
| `--output`  | 输出目录 | `.\output` |
| `--model`   | 分离模型 | `model_bs_roformer_ep_317_sdr_12.9755.ckpt` |
| `--fmt`     | 伴奏格式 MP3/FLAC/WAV | `MP3` |
| `--bitrate` | 伴奏 mp3 码率 | `320k` |
| `--dedup`   | 去重方式 `record`（原文件留原地+哈希清单）/ `move`（移到 processed） | `record` |
| `--once`    | 批处理一次后退出 | （默认持续监听） |

---

## 说明 / 注意事项

- **首次运行**会自动下载 BS-Roformer 模型（约几百 MB）到 `models\`，请耐心等待；之后秒加载。
- **完整歌曲的格式**取决于 `.ncm` 内部封装：若内部是无损 FLAC，解密出来就是 `.flac`（无损，更好），伴奏仍按 `--fmt` 导出（默认 mp3）。
- `ncmdump-go.exe` 即使失败也返回退出码 0，所以脚本靠"输出目录是否真的产生音频文件"来判断成功，更可靠。
- 想换更快、更省显存的模型，可加 `--model UVR-MDX-NET-Inst_HQ_3.onnx`（音质略低于 Roformer）。查看全部可用模型：
  ```powershell
  .\.venv\Scripts\python.exe -m audio_separator.separator -l --list_filter vocals
  ```
- 开机自启：把 `run.bat` 做成快捷方式放进"启动"文件夹，或用 Windows 任务计划程序。
