# 微博 POI 签到文本采集器（网页/公开接口抓取）

本项目用于抓取微博带地理标签的内容，从中**动态提取 POI 数据**，并按天输出 CSV 文件，支持断点续爬与重试。

## 功能

- 使用微博接口抓取带地理信息的微博数据，自动提取 POI。
- 输出字段：`poi_id, poi_name, checkin_count, raw_poi`。
- 按天保存 CSV 文件（例如 `data/2024-08-01.csv`），写入前按 `poi_id` 去重。
- 支持断点续爬：进度写入 `state.json`，重复运行不会重复抓。
- 支持 `--min-checkin-count` 筛选最小签到次数（默认 100，仅保留 `checkin_count > min` 的 POI）。
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

- `weibo_data_url`: 微博接口地址（默认 `https://m.weibo.cn/api/container/getIndex`）。
- `headers`: 常用请求头，需要提供 `Cookie`。
- `params`: 可扩展的查询参数（脚本会追加 `page` 与 `date`）。

## 使用方式

```bash
python src/main.py \
  --start-date 2022-01-08 \
  --end-date 2022-08-24 \
  --output-dir data \
  --sleep 1.5 \
  --max-retry 3 \
  --min-checkin-count 100 \
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
  --min-checkin-count 100 \
  --log-level INFO
```

## 输出字段说明

| 字段 | 说明 |
| --- | --- |
| poi_id | POI ID |
| poi_name | POI 名称 |
| checkin_count | 签到次数 |
| raw_poi | 原始 POI 结构 JSON 字符串 |

## 日志说明

程序默认输出以下关键信息：

- 启动信息：日期范围、输出目录、最小签到次数。
- 每日进度：当前处理日期与页码。
- 每页/批次：抓取条数、预计休眠时间。
- 保存信息：写入哪个 CSV、写入条数、去重后总数。
- 异常信息：HTTP 状态码、错误原因、重试次数与是否跳过。

## 断点续爬

- 默认在 `output_dir/state.json` 保存进度。
- 支持 `--state-file` 指定自定义路径。
