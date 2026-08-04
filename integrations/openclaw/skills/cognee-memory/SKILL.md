---
name: cognee-memory
version: 1.0.0
description: AI知识引擎 - 6行代码实现记忆系统。remember/recall/forget/improve循环，向量+图搜索，支持OpenClaw插件。
keywords: [memory,knowledge,graph,vector,search,cognee,ai,rag]
---

# Cognee Memory System
AI知识引擎 - 6行代码实现记忆系统

**官网：** https://cognee.ai  
**GitHub：** https://github.com/topoteretes/cognee  
**安装：** `pip install cognee`  
**OpenClaw插件：** `@cognee/cognee-openclaw`

---

## 核心API

### 四大操作

| 操作 | 功能 | 说明 |
|------|------|------|
| `remember` | 存储记忆 | 永久存储到知识图谱 |
| `recall` | 查询记忆 | 自动路由最优搜索策略 |
| `forget` | 删除记忆 | 删除过时/错误记忆 |
| `improve` | 优化学习 | 持续学习提升准确性 |

---

## 快速开始

### Python API

```python
import cognee
import asyncio

async def main():
    # 存储到知识图谱
    await cognee.remember("Cognee turns documents into AI memory.")
    
    # 存储到会话缓存（快速）
    await cognee.remember("User prefers detailed explanations.", session_id="chat_1")
    
    # 查询（自动路由）
    results = await cognee.recall("What does Cognee do?")
    for result in results:
        print(result)
    
    # 删除
    await cognee.forget(dataset="main_dataset")

asyncio.run(main())
```

### CLI

```bash
cognee-cli remember "Cognee turns documents into AI memory."
cognee-cli recall "What does Cognee do?"
cognee-cli forget --all
cognee-cli -ui  # 打开本地UI
```

---

## 配置

### 环境变量

```bash
# OpenAI API（必需）
export LLM_API_KEY="your-openai-key"

# 或使用其他LLM提供商
# 见: https://docs.cognee.ai/setup-configuration/llm-providers

# Cognee Cloud（可选）
export COGNEE_SERVICE_URL="https://your-instance.cognee.ai"
export COGNEE_API_KEY="ck_..."
```

---

## 使用场景

### 1. 客服Agent
```
用户："我的发票有问题还没解决"
Cognee追踪：历史交互、失败操作、已解决案例、产品历史
Agent回复："找到2个上月类似计费案例已解决，问题由支付系统同步延迟导致"
```

### 2. SQL Copilot（知识蒸馏）
```
用户："如何计算客户留存率？"
Cognee追踪：专家SQL查询、工作流模式、schema结构、成功实现
Agent回复："高级分析师解决了类似留存查询，这是他们的方案..."
```

### 3. 跨会话记忆
```python
# Session 1
await cognee.remember("用户喜欢详细的解释", session_id="user_123")

# Session 2（跨会话查询）
results = await cognee.recall("用户偏好什么？", session_id="user_123")
```

---

## OpenClaw插件安装

```bash
npm install @cognee/cognee-openclaw
```

插件自动集成：
- `SessionStart` → 初始化记忆
- `PostToolUse` → 捕获行动
- `UserPromptSubmit` → 注入相关上下文
- `PreCompact` → 跨上下文保留记忆
- `SessionEnd` → 桥接到永久知识图谱

---

## vs 其他记忆系统

| 功能 | 我们现有 | Cognee |
|------|---------|--------|
| 存储方式 | 文件 | 向量+图双存储 |
| 搜索方式 | 关键词 | 语义+关系 |
| 学习能力 | 无 | forget+improve |
| 跨Agent | 不支持 | 共享知识图谱 |
| 可视化 | 无 | CLI UI |

---

## 部署选项

| 平台 | 说明 |
|------|------|
| Cognee Cloud | 托管服务 |
| Modal | 无服务器，GPU自动扩展 |
| Railway | 简化PaaS |
| Fly.io | 边缘部署 |
| Render | 简单PaaS |

---

## 示例代码

### 完整记忆循环

```python
import cognee
import asyncio

async def memory_loop():
    # 1. 学习新知识
    await cognee.remember("用户正在学习Python编程")
    await cognee.remember("用户偏好边做边学的教学方式")
    
    # 2. 查询相关记忆
    results = await cognee.recall("用户的学习偏好是什么？")
    
    # 3. 根据反馈改进
    await cognee.improve("纠正对用户偏好的错误理解")
    
    # 4. 忘记错误记忆
    await cognee.forget("错误的假设")

asyncio.run(memory_loop())
```

---

## 安装状态

- Python包：✅ 已安装 `cognee`
- OpenClaw插件：需额外安装 `@cognee/cognee-openclaw`

---

*Powered by Cognee | https://cognee.ai*
