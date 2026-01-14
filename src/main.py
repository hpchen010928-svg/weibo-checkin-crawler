#!/usr/bin/env python3
import argparse
import csv
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

import requests
import yaml


@dataclass
class PoiItem:
    poi_id: str
    poi_name: str
    checkin_count: int
    raw_poi: str


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> Dict:
        if not os.path.exists(self.path):
            return {"dates": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except json.JSONDecodeError:
            return {"dates": {}}

    def get_date_state(self, date_str: str) -> Dict:
        return self.data.setdefault("dates", {}).setdefault(date_str, {"page": 1, "finished": False})

    def update_date_state(self, date_str: str, page: int, finished: bool) -> None:
        self.data.setdefault("dates", {})[date_str] = {"page": page, "finished": finished}
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


def iterate_dates(start_date: date, end_date: date) -> Iterable[date]:
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def build_params(config: Dict, date_str: str, page: int) -> Dict[str, str]:
    params = dict(config.get("params", {}))
    params.setdefault("page", str(page))
    params.setdefault("date", date_str)
    return params


def fetch_page(
    session: requests.Session,
    config: Dict,
    date_str: str,
    page: int,
    max_retry: int,
    sleep_seconds: float,
    logger: logging.Logger,
) -> Optional[Dict]:
    url = config.get("weibo_data_url") or config.get("base_url")
    if not url:
        raise ValueError("配置缺少 weibo_data_url")
    params = build_params(config, date_str, page)
    for attempt in range(1, max_retry + 1):
        try:
            response = session.get(url, params=params, timeout=config.get("timeout", 20))
            if response.status_code != 200:
                logger.warning(
                    "HTTP %s: 日期 %s page %s, 重试 %s/%s",
                    response.status_code,
                    date_str,
                    page,
                    attempt,
                    max_retry,
                )
                time.sleep(sleep_seconds)
                continue
            return response.json()
        except requests.RequestException as exc:
            logger.warning(
                "请求失败: %s (日期 %s page %s) 重试 %s/%s",
                exc,
                date_str,
                page,
                attempt,
                max_retry,
            )
            time.sleep(sleep_seconds)
    logger.error("请求失败并跳过: 日期 %s page %s", date_str, page)
    return None


def extract_poi_from_mblog(mblog: Dict) -> Optional[Tuple[str, str, Optional[int], Dict]]:
    poi_id = None
    poi_name = None
    checkin_count = None
    raw: Dict[str, object] = {}

    if "page_info" in mblog:
        page_info = mblog.get("page_info") or {}
        raw["page_info"] = page_info
        if isinstance(page_info, dict):
            poi = page_info.get("poi") or {}
            if isinstance(poi, dict):
                poi_id = poi.get("poiid") or poi.get("poi_id") or poi.get("id")
                poi_name = poi.get("title") or poi.get("name")
                checkin_count = poi.get("checkin_num") or poi.get("checkin_count")

    annotations = mblog.get("annotations")
    if isinstance(annotations, list):
        raw_annotations = []
        for item in annotations:
            if not isinstance(item, dict):
                continue
            place = item.get("place")
            if isinstance(place, dict):
                raw_annotations.append(place)
                poi_id = poi_id or place.get("poiid") or place.get("poi_id") or place.get("id")
                poi_name = poi_name or place.get("title") or place.get("name")
                checkin_count = checkin_count or place.get("checkin_num") or place.get("checkin_count")
        if raw_annotations:
            raw["annotations"] = raw_annotations

    poi_id = str(poi_id).strip() if poi_id is not None else None
    poi_name = str(poi_name).strip() if poi_name is not None else None
    if not poi_id or not poi_name:
        return None

    try:
        checkin_count_value = int(checkin_count) if checkin_count is not None else None
    except (ValueError, TypeError):
        checkin_count_value = None

    return poi_id, poi_name, checkin_count_value, raw


def parse_pois(
    data: Dict,
    min_checkin_count: int,
    logger: logging.Logger,
) -> List[PoiItem]:
    items: List[PoiItem] = []
    cards = data.get("data", {}).get("cards", []) if isinstance(data, dict) else []
    for card in cards:
        mblog = card.get("mblog") if isinstance(card, dict) else None
        if not isinstance(mblog, dict):
            continue
        poi_info = extract_poi_from_mblog(mblog)
        if not poi_info:
            continue
        poi_id, poi_name, checkin_count, raw = poi_info
        if checkin_count is None or checkin_count <= min_checkin_count:
            continue
        items.append(
            PoiItem(
                poi_id=poi_id,
                poi_name=poi_name,
                checkin_count=checkin_count,
                raw_poi=json.dumps(raw, ensure_ascii=False),
            )
        )
    logger.debug("解析到 %s 个 POI", len(items))
    return items


def load_existing_poi_ids(path: str) -> set:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return {row.get("poi_id") for row in reader if row.get("poi_id")}


def write_pois(output_dir: str, date_str: str, items: Iterable[PoiItem], logger: logging.Logger) -> None:
    output_path = os.path.join(output_dir, f"{date_str}.csv")
    os.makedirs(output_dir, exist_ok=True)
    existing_ids = load_existing_poi_ids(output_path)
    new_rows = [row for row in items if row.poi_id not in existing_ids]
    if not new_rows:
        logger.info("保存 %s: 无新增数据", output_path)
        return
    write_header = not os.path.exists(output_path)
    with open(output_path, "a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["poi_id", "poi_name", "checkin_count", "raw_poi"])
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
    parser.add_argument("--min-checkin-count", type=int, default=100, help="最小签到次数")
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

    logger.info(
        "启动: 日期范围 %s ~ %s, 输出目录 %s, 最小签到次数 %s",
        start_date,
        end_date,
        args.output_dir,
        args.min_checkin_count,
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

    for current_date in iterate_dates(start_date, end_date):
        date_str = current_date.isoformat()
        date_state = state.get_date_state(date_str)
        if date_state.get("finished"):
            logger.info("跳过已完成日期 %s", date_str)
            continue
        page = int(date_state.get("page", 1))
        logger.info("开始处理日期 %s", date_str)
        while True:
            logger.info("请求日期 %s 第 %s 页", date_str, page)
            data = fetch_page(session, config, date_str, page, args.max_retry, args.sleep, logger)
            if data is None:
                state.update_date_state(date_str, page, False)
                break
            items = parse_pois(data, args.min_checkin_count, logger)
            if items:
                logger.info(
                    "抓到 %s 个 POI, 预计休眠 %.2f 秒",
                    len(items),
                    args.sleep,
                )
                write_pois(args.output_dir, date_str, items, logger)
            else:
                logger.info("本页无匹配 POI, 预计休眠 %.2f 秒", args.sleep)

            cards = data.get("data", {}).get("cards", []) if isinstance(data, dict) else []
            if not cards:
                logger.info("无更多数据, 日期 %s 完成", date_str)
                state.update_date_state(date_str, page, True)
                break

            page += 1
            state.update_date_state(date_str, page, False)
            time.sleep(args.sleep)


if __name__ == "__main__":
    run()
