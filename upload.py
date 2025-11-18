#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最终增强版 upload.py（2025）
--------------------------------
流水线顺序：
  1. 读取 MP3 元数据（track / artist / album / duration）
  2. 先查 /api/get-cached（内部数据库）
        - 若已有歌词 → 显示并跳过上传，同时移动到 done-*
  3. 再查 /api/get（外部抓取）
        - 若有结果 → 可选择直接使用外部歌词上传
        - 若取消外部上传 → 继续扫描本地 LRC 文件
  4. 寻找本地 LRC 文件（递归，歌手名模糊匹配 + 歌名宽松匹配）
        - 若无 → 打印警告，跳过
  5. 解析 LRC（自动删除网易云“作词/作曲/缩混/母带”类 credit）
        - 若检测到“纯音乐，请欣赏”等 → 上传空歌词标记为纯音乐
  6. 预览 → 上传（使用 lrcup 自动 PoW）
        - 上传成功后移动 MP3 + LRC 到 done-*，并递归删除空文件夹

依赖：
  pip install lrcup mutagen requests
"""

from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path
from typing import Optional, Tuple, List
import difflib

import requests
from mutagen import File as MutaFile
from mutagen.id3 import ID3NoHeaderError
from lrcup import LRCLib


# -------------------- 配置 --------------------

SCRIPT_DIR = Path(__file__).resolve().parent
TRACKS_DIR = SCRIPT_DIR / "tracks"
LRC_DIR = SCRIPT_DIR / "lrc-files"
DONE_LRC_DIR = SCRIPT_DIR / "done-lrc-files"
DONE_TRACK_DIR = SCRIPT_DIR / "done-tracks"

LRCLIB_BASE = "https://lrclib.net/api"
PREVIEW_LINES = 10


# -------------------- 日志 --------------------

def log_info(msg: str) -> None:
    print(f"[INFO] {msg}")


def log_warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def log_error(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)


# -------------------- 通用规范化 --------------------

def normalize_name(s: str) -> str:
    """一般用于艺人名等的轻度规范化：去空格、大小写、全角标点。"""
    s = s.strip().lower()
    replacements = {
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "：": ":",
        "。": ".",
        "，": ",",
        "！": "!",
        "？": "?",
    }
    for k, v in replacements.items():
        s = s.replace(k, v)
    s = re.sub(r"\s+", " ", s)
    return s


def normalize_title_loose(s: str) -> str:
    """
    歌曲名 / 专辑名宽松规范化，用于比较：
      - 删除括号内容 (Remix)、【xxx】、（live）
      - 删除常见后缀 remix / remaster / live / version / ver.
      - 去掉多余符号，只保留字母数字和中文
    不再用 '-' 分割，避免破坏真实标题。
    """
    s = s.lower().strip()

    # 删除括号内容
    s = re.sub(r"[\(\（【\[].*?[\)\）】\]]", "", s)

    # 删除常见无关后缀
    s = re.sub(r"(remix|remaster|live|version|ver\.?)", "", s)

    # 只保留字母数字和中文，其它变空格
    s = re.sub(r"[^\w\u4e00-\u9fa5]+", " ", s)

    # 合并空格
    s = re.sub(r"\s+", " ", s).strip()
    return s


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


# -------------------- metadata 读取 --------------------

class TrackMeta:
    def __init__(self, path: Path, track: str, artist: str, album: str, duration: int):
        self.path = path
        self.track = track
        self.artist = artist
        self.album = album
        self.duration = duration

    def __str__(self) -> str:
        return f"{self.artist} - {self.track} ({self.album}, {self.duration}s)"


def read_track_metadata(mp3_path: Path) -> Optional[TrackMeta]:
    try:
        audio = MutaFile(mp3_path)
        if audio is None or audio.tags is None:
            log_warn(f"无法读取标签: {mp3_path.name}")
            return None
    except ID3NoHeaderError:
        log_warn(f"无 ID3 标签: {mp3_path.name}")
        return None
    except Exception as e:
        log_error(f"读取标签异常 {mp3_path.name}: {e}")
        return None

    tags = audio.tags

    def tag_text(tag):
        f = tags.get(tag)
        return f.text[0] if f and getattr(f, "text", None) else None

    track = tag_text("TIT2")
    artist = tag_text("TPE1")
    album = tag_text("TALB")

    if not track or not artist or not album:
        log_warn(f"标签不完整: {mp3_path.name}")
        return None

    duration = int(round(getattr(audio.info, "length", 0)))
    if duration <= 0:
        log_warn(f"时长无效: {mp3_path.name}")
        return None

    return TrackMeta(mp3_path, track, artist, album, duration)


# -------------------- 艺人拆分 & 匹配 --------------------

def split_artists(s: str) -> List[str]:
    """
    将艺人字符串拆分成多个 artist：
      支持: feat / featuring / ft.、&、和、/、;、、、x、× 等。
    不会把 "alan walker" 拆成 ["alan", "walker"]。
    """
    s = s.strip().lower()

    # feat 变体 → 统一为逗号
    s = re.sub(r"\b(feat|featuring|ft)\.?\s+", ",", s)

    # 常见连接符
    for sep in [" & ", " 和 ", "/", ";", "、", " x ", " × ", " X "]:
        s = s.replace(sep, ",")

    # 汉字逗号
    s = s.replace("，", ",")

    artists = [a.strip() for a in s.split(",") if a.strip()]
    # 去重保持顺序
    seen = set()
    result = []
    for a in artists:
        if a not in seen:
            seen.add(a)
            result.append(a)
    return result


def match_artists(mp3_artists: List[str], lrc_artists: List[str]) -> bool:
    """
    多艺人匹配规则：
      - 对双方艺人列表做 normalize_name
      - 只要有任意一个交集就算成功
    """
    mp3_set = {normalize_name(a) for a in mp3_artists}
    lrc_set = {normalize_name(a) for a in lrc_artists}
    return len(mp3_set.intersection(lrc_set)) > 0


# -------------------- LRC 文件名解析 --------------------

def parse_lrc_filename(path: Path) -> Tuple[List[str], str]:
    """
    LRC 文件名格式固定为：
      Artist - Title.lrc
    这里严格解析，不做任何宽松处理，以免破坏信息。
    """
    stem = path.stem
    if " - " not in stem:
        return [], ""
    artist_raw, title_raw = stem.split(" - ", 1)
    artists = split_artists(artist_raw)
    # title_raw 保留原样，宽松匹配在 compare 阶段做
    return artists, title_raw


# -------------------- LRC 文件匹配（宽松标题 + 艺人匹配） --------------------

def find_lrc_for_track(meta: TrackMeta) -> Optional[Path]:
    """
    在 lrc-files/ 递归查找匹配的 LRC 文件：
      - 文件名解析为 Artist - Title
      - 艺人：只要有一个艺人匹配即成功
      - 标题：使用 normalize_title_loose + 相似度匹配
        * 若 one in another → 视为高度匹配
        * 否则使用 difflib 相似度 >= 0.6
      - 若有多个候选 → 用户选择
    """
    mp3_title_loose = normalize_title_loose(meta.track)
    if not mp3_title_loose:
        return None

    mp3_artists = split_artists(meta.artist)

    candidates: List[Tuple[Path, float]] = []

    for p in LRC_DIR.rglob("*.lrc"):
        lrc_artists, lrc_title_raw = parse_lrc_filename(p)
        if not lrc_title_raw or not lrc_artists:
            continue

        if not match_artists(mp3_artists, lrc_artists):
            continue

        lrc_title_loose = normalize_title_loose(lrc_title_raw)
        if not lrc_title_loose:
            continue

        # 标题匹配：包含优先
        if mp3_title_loose in lrc_title_loose or lrc_title_loose in mp3_title_loose:
            title_sim = 0.95
        else:
            title_sim = similar(mp3_title_loose, lrc_title_loose)

        if title_sim < 0.6:
            continue

        candidates.append((p, title_sim))

    if not candidates:
        return None

    # 按相似度排序
    candidates.sort(key=lambda x: x[1], reverse=True)
    paths = [c[0] for c in candidates]

    if len(paths) == 1:
        return paths[0]

    print("\n匹配到多个歌词文件（已按相似度排序），请选择：")
    for idx, (p, sim) in enumerate(candidates, 1):
        rel = p.relative_to(SCRIPT_DIR)
        print(f"{idx}) {rel}  (title_sim={sim:.2f})")

    while True:
        ch = input(f"请输入 1-{len(paths)}: ").strip()
        if ch.isdigit():
            num = int(ch)
            if 1 <= num <= len(paths):
                return paths[num - 1]
        print("输入无效，请重新输入。")


# -------------------- LRC 内容解析 --------------------

TAG_RE = re.compile(r"\[\d{2}:\d{2}(?:\.\d{1,3})?\]")

# credit 行： [mm:ss.xx]键：值  或  [mm:ss.xx]键 : 值
CREDIT_LINE_RE = re.compile(
    r"^\[\d{2}:\d{2}(?:\.\d{1,3}(?:-\d+)?)?\]\s*[^:\s]+?\s*[:：]\s*.+$"
)

PURE_MUSIC_KEYWORDS = [
    "纯音乐，请欣赏",
    "純音樂，請欣賞",
    "純音樂 請欣賞",
    "純音樂",
    "instrumental",
    "インストゥルメンタル",
]


def read_text_any(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")


def parse_lrc_file(path: Path) -> Tuple[str, str]:
    """
    解析 LRC：
      - 删除所有网易云 credit 行（[时间]键：值 / 键 : 值）
      - 检测“纯音乐，请欣赏”类关键字 → 视为纯音乐，返回空歌词
      - 去掉 [ar:][ti:][al:][by:] 等标签行（仅保留同步内容）
      - 生成 syncedLyrics（原始带时间戳）+ plainLyrics（去时间戳的纯文本）
    """
    raw = read_text_any(path)
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    synced_lines: List[str] = []
    plain_lines: List[str] = []

    is_pure_music = False

    for line in raw.splitlines():
        s = line.strip()

        # 空行直接保留结构
        if not s:
            synced_lines.append("")
            plain_lines.append("")
            continue

        # 检查纯音乐关键词（在去时间戳后）
        s_no_tag = TAG_RE.sub("", s).strip().lower()
        for kw in PURE_MUSIC_KEYWORDS:
            if kw.lower() in s_no_tag:
                is_pure_music = True

        # 删除 credit 行
        if CREDIT_LINE_RE.match(s):
            continue

        # 删除 [ar:], [ti:], [al:], [by:] 等标签行
        if re.match(r"^\[[a-zA-Z]{2,3}:.+\]$", s):
            synced_lines.append(line)
            continue

        # 去掉时间戳，抽 plain 文本
        no_tag = TAG_RE.sub("", s).strip()

        synced_lines.append(line)
        plain_lines.append("" if not no_tag else no_tag)

    # 若检测到纯音乐 → 直接返回空歌词
    if is_pure_music:
        return "", ""

    # 清理 plain 前后空行
    while plain_lines and plain_lines[0] == "":
        plain_lines.pop(0)
    while plain_lines and plain_lines[-1] == "":
        plain_lines.pop()

    synced_text = "\n".join(synced_lines)
    plain_text = "\n".join(plain_lines)
    return synced_text, plain_text


def preview(label: str, text: str, max_lines: int = PREVIEW_LINES) -> None:
    print(f"--- {label} ---")
    if not text:
        print("[空]")
        print("-" * 40)
        return
    lines = text.splitlines()
    for ln in lines[:max_lines]:
        print(ln)
    if len(lines) > max_lines:
        print(f"... 共 {len(lines)} 行")
    print("-" * 40)


# -------------------- API 查询 --------------------

def check_duration(meta: TrackMeta, record: dict, label: str) -> None:
    rec_dur = record.get("duration")
    if rec_dur is None:
        return
    try:
        rec_dur = int(round(float(rec_dur)))
    except Exception:
        return

    diff = abs(rec_dur - meta.duration)
    if diff <= 2:
        log_info(
            f"{label} 时长检查：LRCLIB={rec_dur}s 本地={meta.duration}s 差值={diff}s（<=2s）"
        )
    else:
        log_warn(
            f"{label} 时长检查：LRCLIB={rec_dur}s 本地={meta.duration}s 差值={diff}s（>2s）"
        )


def api_get(meta: TrackMeta, endpoint: str, label: str) -> Optional[dict]:
    params = {
        "track_name": meta.track,
        "artist_name": meta.artist,
        "album_name": meta.album,
        "duration": meta.duration,
    }
    try:
        r = requests.get(f"{LRCLIB_BASE}/{endpoint}", params=params, timeout=10)
    except Exception as e:
        log_warn(f"{label} 请求失败: {e}")
        return None

    if r.status_code == 200:
        data = r.json()
        check_duration(meta, data, label)
        return data
    return None


def get_cached(meta: TrackMeta) -> Optional[dict]:
    return api_get(meta, "get-cached", "内部数据库 (/api/get-cached)")


def get_external(meta: TrackMeta) -> Optional[dict]:
    return api_get(meta, "get", "外部抓取 (/api/get)")


# -------------------- 上传 --------------------

class LRCLibUploader:
    def __init__(self):
        self.client = LRCLib()

    def upload(self, meta: TrackMeta, plain: str, synced: str) -> bool:
        try:
            token = self.client.request_challenge()
            self.client.publish(
                token=token,
                track=meta.track,
                artist=meta.artist,
                album=meta.album,
                duration=meta.duration,
                plain_lyrics=plain,
                synced_lyrics=synced,
            )
            return True
        except Exception as e:
            log_error(f"上传失败：{e}")
            return False


# -------------------- 工具：递归删除空文件夹 --------------------

def delete_empty_dirs(root: Path, keep_root: bool = True) -> None:
    """
    递归删除 root 下的空目录。
    默认保留 root 自身。
    """
    if not root.is_dir():
        return

    # 从层级最深的开始删
    dirs = sorted(
        [d for d in root.rglob("*") if d.is_dir()],
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for d in dirs:
        try:
            next(d.iterdir())
        except StopIteration:
            try:
                d.rmdir()
            except OSError:
                pass

    if not keep_root:
        try:
            next(root.iterdir())
        except StopIteration:
            try:
                root.rmdir()
            except OSError:
                pass


# -------------------- 主流程 --------------------

def move_after_done(meta: TrackMeta, lrc_path: Optional[Path]) -> None:
    """
    上传成功或确认已有歌词之后：
      - 将 MP3 & LRC 移动到 done-*
      - 避免重名覆盖，自动加 _dup
      - 删除空目录
    """
    try:
        if lrc_path and lrc_path.exists():
            target_lrc = DONE_LRC_DIR / lrc_path.name
            if target_lrc.exists():
                target_lrc = target_lrc.with_name(
                    target_lrc.stem + "_dup" + target_lrc.suffix
                )
            lrc_path.rename(target_lrc)
            log_info(f"LRC 已移动到：{target_lrc}")

        if meta.path.exists():
            target_mp3 = DONE_TRACK_DIR / meta.path.name
            if target_mp3.exists():
                target_mp3 = target_mp3.with_name(
                    target_mp3.stem + "_dup" + target_mp3.suffix
                )
            meta.path.rename(target_mp3)
            log_info(f"MP3 已移动到：{target_mp3}")

        delete_empty_dirs(LRC_DIR)
        delete_empty_dirs(TRACKS_DIR)
    except Exception as e:
        log_warn(f"移动文件失败：{e}")


def process_track(meta: TrackMeta, uploader: LRCLibUploader, auto_yes=False, dry_run=False):
    log_info(f"处理：{meta}")

    # 1. 先查内部数据库（get-cached）
    cached = get_cached(meta)
    if cached:
        log_info("内部数据库已存在歌词 → 自动移动 MP3+LRC 并跳过上传")
        lrc_path = find_lrc_for_track(meta)
        move_after_done(meta, lrc_path)

        preview("已有 plainLyrics", cached.get("plainLyrics", ""))
        preview("已有 syncedLyrics", cached.get("syncedLyrics", ""))
        return

    # 2. 再查外部抓取（get）
    ext = get_external(meta)
    if ext:
        log_info("外部来源找到歌词 → 可选择使用外部歌词上传")

        preview("外部 plainLyrics（候选）", ext.get("plainLyrics", ""))
        preview("外部 syncedLyrics（候选）", ext.get("syncedLyrics", ""))

        use_external = False
        if auto_yes:
            use_external = True
        else:
            ch = input("是否使用外部歌词上传？[y/N]: ").strip().lower()
            if ch in ("y", "yes"):
                use_external = True

        if use_external:
            if dry_run:
                log_info("[dry-run] 仅预览外部歌词，不上传")
                return

            plain = ext.get("plainLyrics", "") or ""
            synced = ext.get("syncedLyrics", "") or ""

            if uploader.upload(meta, plain, synced):
                log_info("外部歌词上传完成 ✓")
                # 找一下本地是否有对应 LRC，有的话一起移动
                lrc_path = find_lrc_for_track(meta)
                move_after_done(meta, lrc_path)
            else:
                log_error("外部歌词上传失败 ×")
            return
        else:
            log_info("用户选择不使用外部歌词 → 改为检查本地 LRC 文件")

    # 3. 外部没有 / 用户拒绝外部 → 查本地 LRC
    lrc_path = find_lrc_for_track(meta)
    if not lrc_path:
        log_warn(f"⚠ 未找到本地 LRC 文件 → 跳过该歌曲：{meta.track}")
        return

    # 4. 解析本地 LRC
    synced, plain = parse_lrc_file(lrc_path)
    preview("本地 plainLyrics（将上传）", plain)
    preview("本地 syncedLyrics（将上传）", synced)

    if dry_run:
        log_info("[dry-run] 仅预览本地歌词，不上传")
        return

    if not auto_yes:
        ch = input("确认上传本地歌词？[y/N]: ").strip().lower()
        if ch not in ("y", "yes"):
            log_info("用户取消上传")
            return

    # 5. 上传本地歌词（包括纯音乐的空歌词）
    if uploader.upload(meta, plain, synced):
        log_info("上传完成 ✓")
        move_after_done(meta, lrc_path)
    else:
        log_error("上传失败 ×")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="不询问确认，自动上传")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不上传")
    parser.add_argument(
        "--single", type=str, help="只处理 tracks/ 目录下指定文件名的 MP3"
    )
    args = parser.parse_args()

    DONE_LRC_DIR.mkdir(exist_ok=True)
    DONE_TRACK_DIR.mkdir(exist_ok=True)

    uploader = LRCLibUploader()

    if args.single:
        mp3 = TRACKS_DIR / args.single
        if not mp3.is_file():
            log_error(f"文件不存在：{mp3}")
            return
        metas = [read_track_metadata(mp3)]
    else:
        metas = [read_track_metadata(p) for p in TRACKS_DIR.rglob("*.mp3")]

    for meta in metas:
        if meta:
            process_track(meta, uploader, auto_yes=args.yes, dry_run=args.dry_run)
            print()

    log_info("全部完成。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] 用户中断执行，已退出。")
        sys.exit(0)