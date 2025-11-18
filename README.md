# 📄 **README**

# LRCLIB Importer

一个用于批量处理本地音乐库的歌词上传工具。  
它会自动读取 MP3 的 metadata，查询 LRCLIB 数据库，如果没有对应歌词则自动上传本地或外部抓取到的 LRC。

---

## ✨ 功能特性

- 自动读取 `tracks/` 中 MP3 的真实元数据（曲名 / 歌手 / 专辑 / 时长）
- 优先查询 LRCLIB 内部数据库（`/api/get-cached`）
- 若外部数据库（`/api/get`）有歌词 → 自动使用外部版本上传
- 若无外部歌词 → 使用本地 `lrc-files/` 中的 LRC 文件
- 歌手名模糊匹配、递归搜索 LRC
- 自动清除网易云歌词的“作词 / 作曲”行
- 上传成功后自动移动文件：
  - MP3 → `done-tracks/`
  - LRC → `done-lrc-files/`
- 支持 dry-run 和单文件处理
- 使用 `lrcup` 自动处理发布所需的 Proof-of-Work

---

## 📦 安装依赖

```bash
pip install -r requirements.txt
````

---

## 🚀 使用方式

### 批量处理整个音乐库

```bash
python upload.py
```

### 不需要确认（自动上传）

```bash
python upload.py --yes
```

### 只处理单个 MP3 文件

```bash
python upload.py --single "example.mp3"
```

### 仅预览，不实际上传

```bash
python upload.py --dry-run
```

---

## 📁 项目结构

```
lrclib-importer/
├── tracks/             # 本地 MP3 文件（递归扫描）
├── lrc-files/          # 本地 LRC 文件（Artist - Title.lrc）
├── done-tracks/        # 上传成功后自动归档
├── done-lrc-files/     # 上传成功后归档 LRC
├── upload.py           # 主脚本
├── requirements.txt    # Python 依赖
└── README.md
```

---

## 💡 适用场景

* 配合 Jellyfin + lrclib 插件手动补齐歌词
* 补充 LRCLIB 中缺失的歌词
* 大量本地音乐自动匹配歌词（外部抓取 + 本地 LRC）
* 想清理音乐库、自动归档处理过的文件

---

## 📝 许可证

MIT License