# 远端字幕清除服务接口约定

## 目的与边界

`subtitle_cleanup` 使用远端部署的 [YaoFANGUK/video-subtitle-remover](https://github.com/YaoFANGUK/video-subtitle-remover) 清除视频中的硬字幕。

本服务只是该开源项目外的一层任务型 Web API：负责接收素材、排队执行、暴露状态与日志、提供处理结果下载。字幕清除算法、模型和视频修复均由 `video-subtitle-remover` 执行。

Workbench 与字幕清除服务不共享本机文件系统。因此，接口不得传递本机磁盘路径；视频通过上传进入远端服务，处理完成后由 Workbench 下载回本地任务目录。

服务不理解短剧、解说任务、目标语言、解说骨架或 `clip_plan`。这些业务编排由 Workbench 负责。

## 调用流程

```text
Workbench
  1. 按已审核的 clip_timeline 生成待清除的无音频视频
  2. 上传视频，取得 input_asset_id
  3. 创建字幕清除任务，取得 job_id
  4. 查询任务状态并读取增量日志
  5. 成功后下载 output_asset_id 对应的视频
  6. 将清除结果保存到本地任务目录，供 render_commentary_video 使用

远端字幕清除服务
  上传素材 -> 排队 -> 调用 video-subtitle-remover -> 保存结果 -> 提供下载
```

服务默认最多同时执行一个清除任务，其他任务排队，避免多个模型进程争抢 GPU、CPU、内存和磁盘。

## 通用约定

- API 前缀：`/api/v1`。
- 任务状态：`queued`、`running`、`succeeded`、`failed`、`cancelled`、`cancelling`。
- 所有时间使用 ISO 8601 UTC 格式。
- 所有返回体使用 JSON，视频下载接口除外。
- 服务端必须校验上传素材是可读取的视频，并在创建任务时校验字幕坐标位于视频范围内。
- 服务端不得覆盖上传的原始视频；输出始终为新的远端素材。
- 第一版不提供自动过期清理；Workbench 下载成功后应主动请求删除输入和输出素材，以释放远端磁盘空间。

## 1. 健康检查

```text
GET /health
```

用于 Workbench 判断 HTTP 服务是否可访问，以及展示当前服务版本、已封装的 VSR 能力和支持的修复模式。

`vsr_available` 是当前服务版本是否包含 VSR 执行器的能力标记。第一版服务能够启动时固定返回 `true`；实际模型加载或单个任务的运行错误通过任务状态和任务日志返回，而不通过健康检查预先探测。

响应示例：

```json
{
  "status": "ok",
  "version": "1.0.0",
  "vsr_available": true,
  "supported_inpaint_modes": [
    "sttn-auto",
    "sttn-det",
    "lama",
    "propainter",
    "opencv"
  ],
  "max_concurrency": 1
}
```

## 2. 上传输入视频

```text
POST /api/v1/assets
Content-Type: multipart/form-data
```

请求字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file` | 文件 | 是 | 待清除的视频。由 Workbench 根据已审核时间线生成。 |

响应示例：

```json
{
  "asset_id": "asset_01J...",
  "filename": "clip_timeline_source.mp4",
  "size_bytes": 524288000,
  "video": {
    "width": 1920,
    "height": 1080,
    "fps": 25,
    "duration_seconds": 321.84
  },
  "created_at": "2026-07-16T12:00:00Z"
}
```

第一版可使用普通 `multipart/form-data` 上传。视频通常较大，后续可在不改变任务接口的前提下增加分片、断点续传上传。

## 3. 创建字幕清除任务

```text
POST /api/v1/jobs
Content-Type: application/json
```

请求示例：

```json
{
  "input_asset_id": "asset_01J...",
  "subtitle_areas": [
    {
      "ymin": 1510,
      "ymax": 1740,
      "xmin": 120,
      "xmax": 1800
    }
  ],
  "inpaint_mode": "sttn-auto"
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `input_asset_id` | string | 是 | 上传接口返回的远端视频素材 ID。 |
| `subtitle_areas` | array | 是 | 一个或多个字幕清除区域。每项均使用像素坐标。 |
| `subtitle_areas[].ymin` | integer | 是 | 区域上边界。 |
| `subtitle_areas[].ymax` | integer | 是 | 区域下边界。 |
| `subtitle_areas[].xmin` | integer | 是 | 区域左边界。 |
| `subtitle_areas[].xmax` | integer | 是 | 区域右边界。 |
| `inpaint_mode` | string | 否 | `video-subtitle-remover` 修复模式；默认 `sttn-auto`。 |

坐标顺序与 `video-subtitle-remover` 的命令行参数一致：`ymin ymax xmin xmax`。可传多个区域，以支持双语字幕、台标等需要同时清除的文本区域。

响应示例：

```json
{
  "job_id": "subclean_01J...",
  "status": "queued",
  "created_at": "2026-07-16T12:00:00Z"
}
```

## 4. 查询任务状态

```text
GET /api/v1/jobs/{job_id}
```

执行中响应示例：

```json
{
  "job_id": "subclean_01J...",
  "status": "running",
  "stage": "inpainting",
  "progress": 42,
  "message": "正在处理第 1840 / 4380 帧",
  "created_at": "2026-07-16T12:00:00Z",
  "started_at": "2026-07-16T12:00:05Z"
}
```

成功响应示例：

```json
{
  "job_id": "subclean_01J...",
  "status": "succeeded",
  "stage": "completed",
  "progress": 100,
  "output_asset_id": "asset_01K...",
  "video": {
    "width": 1920,
    "height": 1080,
    "fps": 25,
    "duration_seconds": 321.84
  },
  "created_at": "2026-07-16T12:00:00Z",
  "started_at": "2026-07-16T12:00:05Z",
  "completed_at": "2026-07-16T12:18:43Z"
}
```

失败响应示例：

```json
{
  "job_id": "subclean_01J...",
  "status": "failed",
  "error_code": "VSR_PROCESS_FAILED",
  "message": "字幕清除程序退出，详情见任务日志。",
  "created_at": "2026-07-16T12:00:00Z",
  "started_at": "2026-07-16T12:00:05Z",
  "completed_at": "2026-07-16T12:03:41Z"
}
```

`progress` 无法准确获得时可以省略；不得返回虚假的处理百分比。此时应以 `stage`、`message` 和日志表示进度。

## 5. 读取增量日志

```text
GET /api/v1/jobs/{job_id}/logs?after={sequence}
```

`after` 默认为 `0`，表示读取从第一条开始的日志；每次返回 `next_after`，Workbench 在下一次请求时传回该值。

响应示例：

```json
{
  "next_after": 18,
  "items": [
    {
      "sequence": 17,
      "timestamp": "2026-07-16T12:03:10Z",
      "level": "info",
      "message": "开始使用 sttn-auto 清除字幕。"
    }
  ]
}
```

日志级别为 `debug`、`info`、`warning` 或 `error`。

## 6. 取消任务

```text
POST /api/v1/jobs/{job_id}/cancel
```

服务应终止尚未开始的排队任务；对于正在执行的任务，应尽快终止底层 `video-subtitle-remover` 进程，并将任务状态最终置为 `cancelled`。

响应示例：

```json
{
  "job_id": "subclean_01J...",
  "status": "cancelling"
}
```

## 7. 下载处理结果

```text
GET /api/v1/assets/{asset_id}/download
```

仅允许下载已成功任务返回的 `output_asset_id`。第一版 VSR 服务只产生 MP4 视频，响应为 `video/mp4` 文件流，并提供输出文件名。

Workbench 下载完成后，将其保存为本地 `subtitle_cleanup` 产物；后续 `render_commentary_video` 从本地产物读取清除后的视频，再合成 TTS 和解说字幕。

## 8. 删除远端素材

```text
DELETE /api/v1/assets/{asset_id}
```

用于删除已上传的输入素材或已下载的输出素材，释放远端磁盘空间。

当前约束：

- 正在被任务使用的输入素材不能删除。
- 删除为幂等操作：素材已经不存在时仍可返回成功或明确的 `not_found` 状态。
- 第一版不跟踪下载中的文件，也不会在后台自动删除过期素材；调用方应在下载成功后删除不再需要的输入和输出素材。

## Workbench 集成责任边界

Workbench 负责：

1. 依据已审核的 `clip_timeline` 生成待清除的无音频视频。
2. 将用户标定的字幕矩形转换为该输入视频的像素坐标。
3. 调用上传、创建任务、状态、日志、取消和下载接口。
4. 下载成功结果并写入本地任务目录。
5. 在 `subtitle_cleanup` 的结果页展示执行日志、处理状态和处理后视频。

远端服务负责：

1. 素材临时存储与清理。
2. 任务排队、执行、取消与日志。
3. 调用 `video-subtitle-remover` 并生成新的无字幕视频。
4. 产物元数据校验与下载。
