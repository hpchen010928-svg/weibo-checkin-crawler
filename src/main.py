#!/usr/bin/env python3
import argparse
import csv
import hashlib
import html
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, date
from typing import Dict, Iterable, List, Optional, Tuple

import requests
import yaml
from dateutil import parser as date_parser


@dataclass
class Poi:
    poi_id: str
    poi_name: str
    checkin_count: int


@dataclass
class WeiboItem:
    weibo_id: str
    text: str
    created_at: str
    poi_id: str
    poi_name: str
    lat: Optional[str]
    lng: Optional[str]
    raw_poi: str
    user_id_hash: Optional[str]


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> Dict:
        if not os.path.exists(self.path):
            return {"pois": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except json.JSONDecodeError:
            return {"pois": {}}

    def get_poi_state(self, poi_id: str) -> Dict:
        return self.data.setdefault("pois", {}).setdefault(
            poi_id,
            {"page": 1, "finished": False, "last_date": None},
        )

    def update_poi_state(self, poi_id: str, page: int, finished: bool, last_date: Optional[str]) -> None:
        self.data.setdefault("pois", {})[poi_id] = {
            "page": page,
            "finished": finished,
            "last_date": last_date,
        }
        self.save()

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)


def load_config(path: str) -> Dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_pois(path: str) -> List[Poi]:
    pois: List[Poi] = []
    with open(path, "r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                checkin_count = int(row.get("checkin_count", 0))
            except ValueError:
                continue
            if checkin_count <= 100:
                continue
            poi_id = str(row.get("poi_id", "")).strip()
            poi_name = str(row.get("poi_name", "")).strip()
            if not poi_id:
                continue
            pois.append(Poi(poi_id=poi_id, poi_name=poi_name, checkin_count=checkin_count))
    return pois


def normalize_text(text: str) -> str:
    cleaned = html.unescape(text)
    return "".join(part for part in cleaned.splitlines()).replace("\u200b", "").strip()


def strip_html_tags(text: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", text)


def parse_created_at(raw: str) -> Optional[datetime]:
    try:
        return date_parser.parse(raw)
    except (ValueError, TypeError):
        return None


def extract_lat_lng(mblog: Dict) -> Tuple[Optional[str], Optional[str]]:
    geo = mblog.get("geo")
    if isinstance(geo, str):
        parts = geo.split()
        if len(parts) == 2:
            return parts[1], parts[0]
    if isinstance(geo, dict):
        coords = geo.get("coordinates")
        if isinstance(coords, list) and len(coords) == 2:
            return str(coords[0]), str(coords[1])
    annotations = mblog.get("annotations")
    if isinstance(annotations, list):
        for item in annotations:
            place = item.get("place") if isinstance(item, dict) else None
            if isinstance(place, dict):
                lat = place.get("latitude")
                lng = place.get("longitude")
                if lat is not None and lng is not None:
                    return str(lat), str(lng)
    return None, None


def extract_raw_poi(mblog: Dict) -> str:
    raw: Dict[str, object] = {}
    for key in ("poiid", "page_info", "geo", "annotations"):
        if key in mblog:
            raw[key] = mblog.get(key)
    return json.dumps(raw, ensure_ascii=False)


def hash_user_id(mblog: Dict) -> Optional[str]:
    user = mblog.get("user")
    if not isinstance(user, dict):
        return None
    user_id = user.get("idstr") or user.get("id")
    if not user_id:
        return None
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()


def build_params(config: Dict, poi_id: str, page: int) -> Dict[str, str]:
    params = dict(config.get("params", {}))
    container_template = config.get("poi_container_template", "{poi_id}")
    params.setdefault("containerid", container_template.format(poi_id=poi_id))
    params.setdefault("page", str(page))
    params.setdefault("count", str(config.get("count", 20)))
    return params


def fetch_page(
    session: requests.Session,
    config: Dict,
    poi_id: str,
    page: int,
    max_retry: int,
    sleep_seconds: float,
    logger: logging.Logger,
) -> Optional[Dict]:
    url = config.get("base_url")
    if not url:
        raise ValueError("配置缺少 base_url")
    params = build_params(config, poi_id, page)
    for attempt in range(1, max_retry + 1):
        try:
            response = session.get(url, params=params, timeout=config.get("timeout", 20))
            if response.status_code != 200:
                logger.warning(
                    "HTTP %s: POI %s page %s, 重试 %s/%s",
                    response.status_code,
                    poi_id,
                    page,
                    attempt,
                    max_retry,
                )
                time.sleep(sleep_seconds)
                continue
            return response.json()
        except requests.RequestException as exc:
            logger.warning(
                "请求失败: %s (POI %s page %s) 重试 %s/%s",
                exc,
                poi_id,
                page,
                attempt,
                max_retry,
            )
            time.sleep(sleep_seconds)
    logger.error("请求失败并跳过: POI %s page %s", poi_id, page)
    return None


def parse_items(
    data: Dict,
    poi: Poi,
    start_date: date,
    end_date: date,
    logger: logging.Logger,
) -> Tuple[List[WeiboItem], bool, Optional[str]]:
    items: List[WeiboItem] = []
    cards = data.get("data", {}).get("cards", []) if isinstance(data, dict) else []
    reached_start = False
    min_date: Optional[date] = None
    for card in cards:
        mblog = card.get("mblog") if isinstance(card, dict) else None
        if not isinstance(mblog, dict):
            continue
        created_raw = mblog.get("created_at")
        created_dt = parse_created_at(created_raw)
        if not created_dt:
            continue
        created_date = created_dt.date()
        if min_date is None or created_date < min_date:
            min_date = created_date
        if created_date < start_date:
            reached_start = True
            break
        if created_date > end_date:
            continue
        text_raw = mblog.get("text", "")
        text = strip_html_tags(normalize_text(text_raw))
        weibo_id = str(mblog.get("id") or mblog.get("idstr") or "")
        if not weibo_id:
            continue
        lat, lng = extract_lat_lng(mblog)
        raw_poi = extract_raw_poi(mblog)
        user_hash = hash_user_id(mblog)
        items.append(
            WeiboItem(
                weibo_id=weibo_id,
                text=text,
                created_at=created_dt.isoformat(),
                poi_id=poi.poi_id,
                poi_name=poi.poi_name,
                lat=lat,
                lng=lng,
                raw_poi=raw_poi,
                user_id_hash=user_hash,
            )
        )
    logger.debug("解析到 %s 条微博", len(items))
    return items, reached_start, min_date.isoformat() if min_date else None


def load_existing_ids(path: str) -> set:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return {row.get("weibo_id") for row in reader if row.get("weibo_id")}


def write_items(
    output_dir: str,
    items: Iterable[WeiboItem],
    logger: logging.Logger,
    poi_index: int,
    poi_total: int,
) -> None:
    grouped: Dict[str, List[WeiboItem]] = {}
    for item in items:
        date_str = item.created_at[:10]
        grouped.setdefault(date_str, []).append(item)
    for date_str, rows in grouped.items():
        logger.info("开始处理日期 %s, POI %s/%s", date_str, poi_index, poi_total)
        output_path = os.path.join(output_dir, f"{date_str}.csv")
        os.makedirs(output_dir, exist_ok=True)
        existing_ids = load_existing_ids(output_path)
        new_rows = [row for row in rows if row.weibo_id not in existing_ids]
        if not new_rows:
            logger.info("保存 %s: 无新增数据", output_path)
            continue
        write_header = not os.path.exists(output_path)
        with open(output_path, "a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "weibo_id",
                    "text",
                    "created_at",
                    "poi_id",
                    "poi_name",
                    "lat",
                    "lng",
                    "raw_poi",
                    "user_id_hash",
                ],
            )
            if write_header:
                writer.writeheader()
            for row in new_rows:
                writer.writerow(row.__dict__)
        logger.info(
            "写入 %s: 新增 %s 条, 去重后总数 %s",
            output_path,
            len(new_rows),
            len(existing_ids) + len(new_rows),
        )


def setup_logger(level: str) -> logging.Logger:
    logger = logging.getLogger("weibo_poi_crawler")
    logger.setLevel(level)
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    logger.handlers = [handler]
    return logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="微博 POI 签到文本采集器")
    parser.add_argument("--start-date", required=True, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--output-dir", required=True, help="输出目录")
    parser.add_argument("--sleep", type=float, default=1.0, help="请求间隔秒数")
    parser.add_argument("--max-retry", type=int, default=3, help="最大重试次数")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--state-file", default=None, help="断点状态文件路径")
    parser.add_argument("--log-level", default="INFO", help="日志等级 DEBUG/INFO/WARNING")
    return parser.parse_args()


def run() -> None:
    args = parse_args()
    logger = setup_logger(args.log_level.upper())
    config = load_config(args.config)

    start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    if start_date > end_date:
        raise ValueError("start_date 不能晚于 end_date")

    pois = load_pois(config.get("poi_file", "pois.csv"))
    logger.info(
        "启动: 读取到 %s 个 POI, 日期范围 %s ~ %s, 输出目录 %s",
        len(pois),
        start_date,
        end_date,
        args.output_dir,
    )

    state_file = args.state_file or os.path.join(args.output_dir, "state.json")
    state = StateStore(state_file)

    session = requests.Session()
    headers = config.get("headers") or {}
    cookies = config.get("cookies") or {}
    if headers:
        session.headers.update(headers)
    if cookies:
        session.cookies.update(cookies)

    for index, poi in enumerate(pois, start=1):
        poi_state = state.get_poi_state(poi.poi_id)
        if poi_state.get("finished"):
            logger.info("跳过已完成 POI %s (%s/%s)", poi.poi_id, index, len(pois))
            continue
        page = int(poi_state.get("page", 1))
        total_written = 0
        logger.info("开始处理 POI %s (%s/%s)", poi.poi_id, index, len(pois))
        while True:
            logger.info("请求 POI %s 第 %s 页", poi.poi_id, page)
            data = fetch_page(session, config, poi.poi_id, page, args.max_retry, args.sleep, logger)
            if data is None:
                state.update_poi_state(poi.poi_id, page, False, poi_state.get("last_date"))
                break
            items, reached_start, last_date = parse_items(data, poi, start_date, end_date, logger)
            if items:
                total_written += len(items)
                logger.info(
                    "抓到 %s 条, 累计 %s 条, 预计休眠 %.2f 秒",
                    len(items),
                    total_written,
                    args.sleep,
                )
                write_items(args.output_dir, items, logger, index, len(pois))
            else:
                logger.info("本页无匹配数据, 预计休眠 %.2f 秒", args.sleep)

            if reached_start:
                logger.info("已到达开始日期, POI %s 完成", poi.poi_id)
                state.update_poi_state(poi.poi_id, page, True, last_date)
                break

            cards = data.get("data", {}).get("cards", []) if isinstance(data, dict) else []
            if not cards:
                logger.info("无更多数据, POI %s 完成", poi.poi_id)
                state.update_poi_state(poi.poi_id, page, True, last_date)
                break

            page += 1
            state.update_poi_state(poi.poi_id, page, False, last_date)
            time.sleep(args.sleep)


if __name__ == "__main__":
    run()
