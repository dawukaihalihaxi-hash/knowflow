# 铁路旅客服务 RAG：第一阶段资料包

先读quality-report.md。共有7份可导入Markdown；另有1份2026企业客规候选被隔离。只读取manifest.documents中ingestion_allowed=true的记录，**禁止扫描整个目录自动入库**。

- raw/：官方网站响应原始字节、网页直接链接的DOC/PDF，以及2026移动页所引用JS。
- corpus/：六份基础来源及2025公告的结构化Markdown。
- evidence/：有效性目录、2026矛盾对照、候选Markdown、补充官方PDF及机器比对结果。
- manifest.json：稳定文档ID、机关/日期/文号/类型/有效性、来源URL、入库许可、SHA256。
- scripts/：配置、抓取/提取、补充核验与完整性验证。

## 复现

使用Python 3.10以上；建议在本地虚拟环境安装依赖。

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r scripts/requirements.txt
python3 scripts/prepare.py --offline
python3 scripts/check.py --offline
python3 scripts/verify.py
```

去掉--offline可重新从官方源抓取。先复制本包为新版本再在线运行；网页可能改变，核验脚本的预期条数及人工报告须重新审查，不能自动更新生产资料。普通HTTP请求使用浏览器User-Agent兼容官网，未登录或使用账号凭据。网络失败直接报错。

verify.py验证manifest的原文与corpus哈希、补充证据哈希及目录/通知编号。离线重建不改变raw或首次下载时间。evidence中的PDF文本与JSON可重建，SHA256SUMS是交付时静态快照，重新生成后需重建清单。

## 多租户独立部署准备

本包是公共参考资料的离线快照，不含个人信息、API密钥、向量或租户配置。后续每租户复制独立数据目录、manifest版本及索引；tenant_id由部署端强制赋值，不能依赖用户输入。metadata里的shared_reference仅描述资料来源，不授权跨租户查询。

未来分块使用document_id+版本原文hash+章/条/款路径作为稳定引用，保留条内上下文，避免把项与适用条件拆散。每租户独立存储路径、访问凭据与向量集合，采用ingestion_allowed及规则类型/日期过滤。当前无分块与租户运行组件；本说明为下一阶段接口约定。
