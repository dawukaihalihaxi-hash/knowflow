# 知序 KnowFlow

个人作品 · 可配置的 AI 知识助手构建与管理平台（MVP）。

## 体验

线上演示：https://knowflow-kparis.onrender.com/ 。作品演示站支持限额匿名创建、上传、构建与发布；出行通为只读样例，管理员登录后可管理。模型费用由部署者账号承担；仓库不包含模型或数据库密钥。

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

仓库提供`render.yaml`，使用免费Web Service和Gunicorn。`APP_PUBLIC_TRIAL=1`允许匿名访客创建一个助手、上传最多10份资料并构建发布。先在已有Supabase项目执行`supabase/migrations/002_anonymous_trial.sql`，并创建`config/trial-limits.json`指定的私有Storage bucket（禁止公开，不配置浏览器访问策略）。配额与工作空间表只授权service_role。每份5MB、累计20MB；每天3次构建、20次问答；7天到期停用。全站容量和模型工作量限额见该配置。三个服务凭据仅在Render配置；管理员口令与会话密钥由Render生成，不能放进源码。

免费实例会休眠，首次访问可能等待唤醒。试用模式将配置、附件、版本与向量缓存保存到同一Supabase项目中的私有Storage，数据库原子保存清单及额度；本地文件仅为缓存。匿名身份保存在浏览器签名Cookie中，清除后无法找回管理权。共享链接仅开放问答，不公开原始附件；回答仍可能引用上传内容，发布前应确认。额度限制不等于人民币账单硬上限。

管理员通过网站独立口令登录，不使用电脑密码或IP识别。管理入口为`/login`。口令仅保存在Render环境变量中，不能写入Git。不要变更APP_SESSION_SECRET，否则既有匿名身份无法继续管理助手。单实例单worker部署；数据库租约避免部署切换期间多个实例同时写入。

## 离线验证

```sh
.venv/bin/python -m unittest discover -s tests -q
```

测试使用临时目录和模拟服务，不调用付费模型，不代表真实问答效果已经通过全面评估。

## 技术栈与边界

Python / Flask / Gunicorn / RAG / Supabase / 百炼模型适配。模型、资料、业务Prompt均通过配置管理。引用校验能检查所引文字是否存在，不能保证语义结论正确；重要事项需结合官方原文核实。
