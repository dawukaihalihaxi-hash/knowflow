# 知序 KnowFlow

个人作品 · 可配置的 AI 知识助手构建与管理平台（MVP）。

## 体验

线上地址在部署验收完成后补充。作品演示站使用固定知识库，管理页面只读，可打开出行通体验真实问答。模型费用由部署者账号承担；仓库不包含模型或数据库密钥。

## 已实现能力

- 通用助手工作台，品牌、知识库、模型、检索和回答设置相互分离。
- PDF、DOCX、Excel、CSV、TXT等资料解析；五列表格管理、原文预览、更新上传、启停与删除。
- 知识切片、向量与关键词融合检索、证据上下文组装、回答及引用校验。
- 构建阶段进度、兼容向量复用、候选版本预览和显式发布；旧版本在更新期间继续服务。
- 服务端管理员权限、参数校验、CSRF与请求限流。

## 演示知识库

出行通是平台的首个场景。快照包含7份允许入库的铁路旅客服务公开资料，234个全部片段，其中202个用于检索。来源与日期见`knowledge/railway/manifest.json`。隔离候选不参与检索。仓库不包含任何私人上传文件。

## 本机运行

Python 3.13或更高版本：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
APP_CONFIG=config/rag.railway.json .venv/bin/python app.py
```

页面可在本机8085打开。真实问答需在运行环境配置DASHSCOPE_API_KEY、SUPABASE_URL、SUPABASE_SECRET_KEY；同时需要已就绪且与配置匹配的知识索引。仅有源码不等于云端索引已经建立。更换Embedding模型／维度需单独构建新索引。程序启动不会自动向量化。

## Render部署

仓库提供`render.yaml`，使用免费Web Service和Gunicorn。`APP_PUBLIC_DEMO=1`允许访客查看固定平台并进行问答，同时在服务端拒绝所有管理写操作。三个服务凭据仅在Render配置；管理员口令与会话密钥由Render生成，不能放进源码。

免费实例会休眠，首次访问可能等待唤醒；运行时文件在重启或重新部署后可能丢失，因此本版本不是持久化生产服务。关闭演示模式用于正式管理前，需处理持久化和访问权限。请求限流不等于费用预算上限。

## 离线验证

```sh
.venv/bin/python -m unittest discover -s tests -q
```

测试使用临时目录和模拟服务，不调用付费模型，不代表真实问答效果已经通过全面评估。

## 技术栈与边界

Python / Flask / Gunicorn / RAG / Supabase / 百炼模型适配。模型、资料、业务Prompt均通过配置管理。引用校验能检查所引文字是否存在，不能保证语义结论正确；重要事项需结合官方原文核实。
