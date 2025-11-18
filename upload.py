#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最终增强版 upload.py（2025）
--------------------------------
流水线顺序（符合用户要求）：
  1. 读取 MP3 元数据（track/artist/album/duration）
  2. 先查 /api/get-cached（内部数据库）
        - 若已有歌词 → 显示并跳过上传
  3. 再查 /api/get（外部抓取）
        - 若有结果 → 显示供参考，但不视为已有
  4. 寻找本地 LRC 文件（递归，歌手名模糊匹配）
        - 若无 → 打印明显警告（选项 2）
  5. 解析 LRC（自动删除网易云“作词/作曲”行）
  6. 预览 → 上传（使用 lrcup 自动 PoW）

特色功能：
  - 递归扫描 tracks/ 和 lrc-files/
  - 歌手名多源拆分，强力模糊匹配
  - 多 LRC 文件自动让用户选择
  - /api/get 与 /api/get-cached 双查询
  - 检查 duration ±2 秒合法性提示
  - 删除网易云作词/作曲行
"""

from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

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


# -------------------- 规范化辅助 --------------------

def normalize_name(s: str) -> str:
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


# -------------------- 歌手名处理 --------------------

def split_artists(s: str) -> list[str]:
    """
    将歌手字符串分割成多个 artist。
    支持逗号、&、x、X、/、feat、featuring、; 等。
    """
    s = s.lower()
    s = re.sub(r"\bfeat\.?\b", ",", s)
    s = re.sub(r"\bfeaturing\b", ",", s)

    for sep in ["&", "和", "/", ";", "、", " x ", " X ", "×"]:
        s = s.replace(sep, ",")

    for sep in ["，", "､"]:
        s = s.replace(sep, ",")

    artists = [a.strip() for a in s.split(",") if a.strip()]
    return list(set(artists))


def match_artists(mp3_artists: list[str], lrc_artists: list[str]) -> bool:
    mp3_norm = [normalize_name(a) for a in mp3_artists]
    lrc_norm = [normalize_name(a) for a in lrc_artists]
    return any(a in lrc_norm for a in mp3_norm)


# -------------------- LRC 文件名解析 --------------------

def parse_lrc_filename(path: Path) -> tuple[list[str], str]:
    stem = path.stem
    if " - " not in stem:
        return [], ""
    a_raw, t_raw = stem.split(" - ", 1)
    return split_artists(a_raw), normalize_name(t_raw)


# -------------------- LRC 文件匹配 --------------------

def find_lrc_for_track(meta: TrackMeta) -> Optional[Path]:
    meta_title = normalize_name(meta.track)
    meta_artists = split_artists(meta.artist)

    candidates = []
    for p in LRC_DIR.rglob("*.lrc"):
        lrc_artists, lrc_title = parse_lrc_filename(p)
        if lrc_title != meta_title:
            continue
        if match_artists(meta_artists, lrc_artists):
            candidates.append(p)

    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    print("\n匹配到多个歌词文件，请选择：")
    for idx, c in enumerate(candidates, 1):
        print(f"{idx}) {c.relative_to(SCRIPT_DIR)}")

    while True:
        ch = input(f"请输入 1-{len(candidates)}: ").strip()
        if ch.isdigit() and 1 <= int(ch) <= len(candidates):
            return candidates[int(ch) - 1]
        print("输入无效，请重新输入。")


# -------------------- LRC 内容解析 --------------------

TAG_RE = re.compile(r"\[\d{2}:\d{2}(?:\.\d{1,3})?\]")
CREDIT_RE = re.compile(r"^(作词|作曲)\s*[:：]\s*.+$")


def read_text_any(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")


def parse_lrc_file(path: Path) -> Tuple[str, str]:
    raw = read_text_any(path)
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    synced_lines = []
    plain_lines = []

    for line in raw.splitlines():
        s = line.strip()
        if not s:
            synced_lines.append("")
            plain_lines.append("")
            continue

        if re.match(r"^\[[a-zA-Z]{2,3}:.+\]$", s):
            synced_lines.append(line)
            continue

        no_tag = TAG_RE.sub("", s).strip()
        if no_tag and CREDIT_RE.match(no_tag):
            continue

        synced_lines.append(line)
        plain_lines.append("" if not no_tag else no_tag)

    while plain_lines and plain_lines[0] == "":
        plain_lines.pop(0)
    while plain_lines and plain_lines[-1] == "":
        plain_lines.pop()

    return "\n".join(synced_lines), "\n".join(plain_lines)


def preview(label: str, text: str):
    print(f"--- {label} ---")
    if not text:
        print("[空]")
        print("-" * 40)
        return
    lines = text.splitlines()
    for ln in lines[:PREVIEW_LINES]:
        print(ln)
    if len(lines) > PREVIEW_LINES:
        print(f"... 共 {len(lines)} 行")
    print("-" * 40)


# -------------------- API 查询 --------------------

def check_duration(meta: TrackMeta, record: dict, label: str) -> None:
    rec_dur = record.get("duration")
    if rec_dur is None:
        return
    try:
        rec_dur = int(round(float(rec_dur)))
    except:
        return

    diff = abs(rec_dur - meta.duration)
    if diff <= 2:
        log_info(f"{label} 时长检查：LRCLIB={rec_dur}s 本地={meta.duration}s 差值={diff}s（<=2s）")
    else:
        log_warn(f"{label} 时长检查：LRCLIB={rec_dur}s 本地={meta.duration}s 差值={diff}s（>2s）")


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


# -------------------- 主流程 --------------------

def process_track(meta: TrackMeta, uploader: LRCLibUploader, auto_yes=False, dry_run=False):
    log_info(f"处理：{meta}")

    # 1. 先查内部数据库
    cached = get_cached(meta)
    if cached:
        log_info("内部数据库已存在歌词 → 自动移动 MP3+LRC 并跳过上传")

        # 找到本地 LRC（移动规则：API-cached 命中也移动）
        lrc_path = find_lrc_for_track(meta)

        try:
            if lrc_path:
                target_lrc = DONE_LRC_DIR / lrc_path.name
                if target_lrc.exists():
                    target_lrc = target_lrc.with_name(target_lrc.stem + "_dup" + target_lrc.suffix)
                lrc_path.rename(target_lrc)
                log_info(f"LRC 已移动到：{target_lrc}")

            target_mp3 = DONE_TRACK_DIR / meta.path.name
            if target_mp3.exists():
                target_mp3 = target_mp3.with_name(target_mp3.stem + "_dup" + target_mp3.suffix)
            meta.path.rename(target_mp3)
            log_info(f"MP3 已移动到：{target_mp3}")
        except Exception as e:
            log_warn(f"移动文件失败：{e}")

        preview("已有 plainLyrics", cached.get("plainLyrics", ""))
        preview("已有 syncedLyrics", cached.get("syncedLyrics", ""))
        return

    # 2. 查外部抓取（新逻辑：若找到则优先上传外部歌词）
    ext = get_external(meta)
    if ext:
        log_info("外部来源找到歌词 → 将直接上传外部歌词，而不是本地 LRC")

        preview("外部 plainLyrics（即将上传）", ext.get("plainLyrics", ""))
        preview("外部 syncedLyrics（即将上传）", ext.get("syncedLyrics", ""))

        if dry_run:
            log_info("[dry-run] 仅预览，不上传")
            return

        if not auto_yes:
            choice = input("确认上传外部歌词？[y/N]: ").lower()
            if choice not in ("y", "yes"):
                log_info("用户取消上传")
                return

        # 上传外部歌词
        plain = ext.get("plainLyrics", "") or ""
        synced = ext.get("syncedLyrics", "") or ""

        if uploader.upload(meta, plain, synced):
            log_info("外部歌词上传完成 ✓")

            # 找到本地 LRC（如果有就移动，没有也不报错）
            lrc_path = find_lrc_for_track(meta)

            try:
                if lrc_path:
                    target_lrc = DONE_LRC_DIR / lrc_path.name
                    if target_lrc.exists():
                        target_lrc = target_lrc.with_name(target_lrc.stem + "_dup" + target_lrc.suffix)
                    lrc_path.rename(target_lrc)
                    log_info(f"LRC 已移动到：{target_lrc}")

                target_mp3 = DONE_TRACK_DIR / meta.path.name
                if target_mp3.exists():
                    target_mp3 = target_mp3.with_name(target_mp3.stem + "_dup" + target_mp3.suffix)
                meta.path.rename(target_mp3)
                log_info(f"MP3 已移动到：{target_mp3}")

            except Exception as e:
                log_warn(f"移动文件失败：{e}")
        else:
            log_error("外部歌词上传失败 ×")

        return  # 注意：外部上传后直接退出整个流程

    # 3. 未找到外部歌词 → 使用本地 LRC
    lrc_path = find_lrc_for_track(meta)
    if not lrc_path:
        log_warn(f"⚠ 未找到本地 LRC 文件 → 跳过该歌曲：{meta.track}")
        return

    # 4. 解析 LRC
    synced, plain = parse_lrc_file(lrc_path)
    preview("本地 plainLyrics（将上传）", plain)
    preview("本地 syncedLyrics（将上传）", synced)

    if dry_run:
        log_info("[dry-run] 仅预览，不上传")
        return

    if not auto_yes:
        choice = input("确认上传？[y/N]: ").lower()
        if choice not in ("y", "yes"):
            log_info("用户取消上传")
            return

    # 5. 上传本地歌词
    if uploader.upload(meta, plain, synced):
        log_info("上传完成 ✓")
        # 自动移动 LRC 和 MP3
        try:
            target_lrc = DONE_LRC_DIR / lrc_path.name
            target_mp3 = DONE_TRACK_DIR / meta.path.name

            lrc_path.rename(target_lrc)
            meta.path.rename(target_mp3)

            log_info(f"LRC 已移动到：{target_lrc}")
            log_info(f"MP3 已移动到：{target_mp3}")
        except Exception as e:
            log_warn(f"移动文件失败：{e}")
    else:
        log_error("上传失败 ×")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--single", type=str)
    args = parser.parse_args()

    uploader = LRCLibUploader()

    if args.single:
        mp3 = TRACKS_DIR / args.single
        if not mp3.is_file():
            log_error(f"文件不存在：{mp3}")
            return
        metas = [read_track_metadata(mp3)]
    else:
        metas = [
            read_track_metadata(p)
            for p in TRACKS_DIR.rglob("*.mp3")
        ]

    for meta in metas:
        if meta:
            process_track(meta, uploader, auto_yes=args.yes, dry_run=args.dry_run)
            print()

    log_info("全部完成。")


if __name__ == "__main__":
    main()
