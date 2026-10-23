# 只读操作示例

下列业务标识均从此前真实查询结果取得。CLI JSON 可用 `--json -` 从标准输入读取；MCP 用 `{operation, parameters}`。所有命令沿用用户选定的状态目录。

| 类别 | operation | parameters 示例 |
| --- | --- | --- |
| 文档 | doc-search | `{"keywords":["项目"],"limit":5,"doc_types":["doc"]}` |
| 文档正文 | doc-read | `{"docid":"<真实文档标识>","content_type":"text"}` |
| 会议 | meeting-list | `{"begin_time":"2026-10-20 00:00:00","end_time":"2026-10-21 23:59:59","limit":5}` |
| 会议详情 | meeting-get | `{"meeting_ids":[{"meeting_id":"<真实会议标识>"}]}` |
| 通讯录 | contact-search | `{"keywords":["<人员姓名>"]}` |
| 日程 | calendar-list | `{"begin_time":"2026-10-20 00:00:00","end_time":"2026-10-21 23:59:59"}` |
| 日程详情 | calendar-get | `{"schedule_ids":["<真实日程标识>"]}` |
| 邮件 | mail-search | `{"begin_time":"2026-10-20 00:00:00","end_time":"2026-10-21 23:59:59","keywords":["项目"],"limit":5}` |
| 邮件正文 | mail-get | `{"mail_ids":["<真实邮件标识>"]}` |
| 微盘最近文件 | disk-list | `{"limit":5}` |
| 微盘搜索 | disk-search | `{"keywords":["项目"],"limit":5}` |
| 微盘元数据 | disk-get | `{"file_id":"<真实文件标识>"}` |
| 普通表格子表 | sheet-get | `{"docid":"<真实表格标识>"}` |
| 普通表格单元格 | sheet-read | `{"docid":"<真实表格标识>","sheet":"<子表名称>","range":"A1:C3"}` |
| 智能表格子表 | smartsheet-sheets | `{"docid":"<真实表格标识>"}` |
| 智能表格字段 | smartsheet-fields | `{"docid":"<真实表格标识>","sheet_title":"<子表名称>","limit":5}` |
| 智能表格记录 | smartsheet-records | `{"docid":"<真实表格标识>","sheet_title":"<子表名称>","limit":5}` |
| 智能表格选列 | smartsheet-query | `{"docid":"<真实表格标识>","sheet_title":"<子表名称>","field_titles":["<列标题>"],"limit":5}` |

分页只在接口支持时使用返回的 cursor，不自动全量翻页。日程/邮件/会议时间窗最多 31 天；批量标识最多 10 个；普通表格最多 1000 格；智能表格选列最多 20 列/50 行，不接受原始 SQL。通讯录和日程列表不支持 limit/cursor。邮件详情只含正文和附件元数据，不下载附件、不标记已读。

追问示例（仅在原任务为 input_required 且已取得答案后）：

```bash
dayuan reply --wait 60 --json '{"parent_request_id":"project-read-001","request_id":"project-reply-001","answer":"选择候选列表中的第一个群"}'
dayuan result project-reply-001 --wait 60
```
