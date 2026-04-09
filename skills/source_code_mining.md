---
name: source_code_mining
summary: 围绕已发现的源码、JS、备份包或目录索引，系统梳理接口、敏感路径、凭据线索和下一步利用入口。
use_when: 已经拿到源码片段、JS URL、目录 listing、备份文件或泄露仓库，需要把零散源码证据整理成明确的下一步动作。
---
# Source Code Mining

目标是把“已经看到的源码证据”转成“下一步该执行什么函数”。

优先关注：
- API 路由、管理端路径、上传点、调试接口、内部网段地址
- token、secret、password、AK/SK、默认凭据、测试账户
- `sourceMappingURL`、未引用的静态资源、备份文件名、隐藏入口
- 与攻击树当前节点直接相关的参数名、文件名、请求方法、Header 约束

反思时应重点检查：
- 之前是否已经拿到源码证据，却没有继续沿着接口、敏感路径或凭据深挖
- executor_brief 是否把源码里的关键参数、路径、请求方法说清楚了
- 之前动作是否只做了抓取，没有把抓取结果转成更具体的下一步工具调用

规划建议：
- 如果已知 JS URL，优先考虑 `js_source_analyze`
- 如果源码暴露了目录、备份、接口前缀，优先考虑 `web_content_discovery`
- 如果源码提示特定文件路径或配置文件名，优先考虑 `system_command` 做定向检索或文件确认
