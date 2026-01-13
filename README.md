# 微博 POI 签到文本采集器（网页/公开接口抓取）

本项目用于抓取微博 POI 相关签到微博的**文本内容**，并按天输出 CSV 文件，支持断点续爬与重试。

## 功能

- 读取 `pois.csv`（字段：`poi_id, poi_name, checkin_count`），仅处理 `checkin_count > 100` 的 POI。
- 通过网页/公开接口抓取 POI 的签到微博。
- 输出字段：`weibo_id, text, created_at, poi_id, poi_name, lat, lng, raw_poi, user_id_hash`。
- 按天保存 CSV 文件（例如 `data/2024-08-01.csv`），写入前按 `weibo_id` 去重。
- 支持断点续爬：进度写入 `state.json`，重复运行不会重复抓。
- 支持 `--log-level` 输出详细日志。

## 环境准备

```bash
pip install -r requirements.txt
```

## 配置

复制并填写配置：

```bash
cp config.yaml.example config.yaml
```

`config.yaml` 示例字段说明：

- `base_url`: 微博接口地址（默认 `https://m.weibo.cn/api/container/getIndex`）。
- `poi_container_template`: POI containerid 模板，例如 `"100808{poi_id}"` 或仅 `"{poi_id}"`。
- `headers`: 常用请求头，需要提供 `Cookie`。
- `params`: 可扩展的查询参数。

## 使用方式

```bash
python src/main.py \
  --start-date 2022-01-08 \
  --end-date 2022-08-24 \
  --output-dir data \
  --sleep 1.5 \
  --max-retry 3 \
  --log-level INFO
```

继续抓取另一个时间段：

```bash
python src/main.py \
  --start-date 2024-08-01 \
  --end-date 2025-12-31 \
  --output-dir data \
  --sleep 1.5 \
  --max-retry 3 \
  --log-level INFO
```

## 输出字段说明

| 字段 | 说明 |
| --- | --- |
| weibo_id | 微博 ID |
| text | 微博正文（已去除 HTML） |
| created_at | 标准时间（ISO 8601） |
| poi_id | POI ID |
| poi_name | POI 名称 |
| lat | 纬度（如有） |
| lng | 经度（如有） |
| raw_poi | 原始 POI 结构 JSON 字符串 |
| user_id_hash | 用户 ID 哈希（如有） |

## 日志说明

程序默认输出以下关键信息：

- 启动信息：读取 POI 数量、日期范围、输出目录。
- POI 进度：当前处理的 POI 及页码。
- 每页/批次：抓取条数、预计休眠时间。
- 保存信息：写入哪个 CSV、写入条数、去重后总数。
- 异常信息：HTTP 状态码、错误原因、重试次数与是否跳过。

## 断点续爬

- 默认在 `output_dir/state.json` 保存进度。
- 支持 `--state-file` 指定自定义路径。

