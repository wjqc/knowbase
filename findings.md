# Findings
- search 可返回 staging/stale；hook AND 路径没有过滤 staging，OR 路径只需一个词命中。
- reindex 删除数据库，也删除 usage/feedback 日志及 hit_count。
- import 仅标题相似去重，没有来源文件哈希；init 只检测知识目录。
- hit_count 实际为读取次数，helpful 才是有效反馈，不能等同采用。
