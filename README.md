# pagent

一个零依赖的命令行编程 Agent，单文件 900 余行，只依赖 Python 标准库。
对接任意 OpenAI 兼容的 `/chat/completions` 接口，能读写文件、执行命令。

```
> 帮我把 utils.py 里的重复函数合并一下
→ read_file {"path": "utils.py"}
→ shell {"command": "grep -n 'def ' utils.py"}
...
改完了：合并了 3 个重复的 _parse 函数，删掉了 42 行冗余代码。
```

## 要求

- Python 3.8+（无第三方依赖）
- 一个 OpenAI 兼容的接口地址和 API Key

## 快速开始

```bash
# 1. 配置（三个变量，都支持命令行覆盖）
export PAGENT_API_KEY="sk-xxxx"
export PAGENT_MODEL="gpt-4o-mini"
export PAGENT_BASE_URL="https://api.openai.com/v1"   # 用中转服务就换成自己的地址

# 2. 交互模式
python pagent.py

# 3. 单次执行
echo "这个项目是做什么的？" | python pagent.py
```

也可以跳过环境变量，直接改 `pagent.py` 顶部的 `DEFAULT_API_KEY` / `DEFAULT_MODEL` /
`DEFAULT_BASE_URL` 三行。优先级：**命令行参数 > 环境变量 > 代码内默认值**。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PAGENT_API_KEY` | 空 | 必填，接口密钥 |
| `PAGENT_MODEL` | 空 | 必填，模型名 |
| `PAGENT_BASE_URL` | 空 | 必填，接口地址，如 `https://api.openai.com/v1` |
| `PAGENT_WORKDIR` | 当前目录 | 工作目录（文件工具的沙箱根） |
| `PAGENT_ALLOW_OUTSIDE` | 关 | 设为 `1` 放开工作目录限制 |
| `PAGENT_CONTEXT_TOKENS` | `262144` | 上下文预算 |
| `PAGENT_MAX_OUTPUT_TOKENS` | `32768` | 单次最大输出 |
| `PAGENT_TIMEOUT` | `300` | 单次请求超时（秒） |
| `PAGENT_RETRIES` | `3` | 失败重试上限 |
| `PAGENT_RETRIES_429` | `8` | 429 限流单独的重试上限 |

## 命令行参数

```
--base-url URL      覆盖接口地址
--model NAME        覆盖模型名
--context N         上下文 token 预算
--max-output N      单次最大输出 token
--system TEXT       自定义系统提示词
--workdir DIR       工作目录（沙箱根）
--allow-outside     放开工作目录限制
-y, --yes           全部自动批准（跳过所有确认，危险）
--no-tools          禁用工具调用，纯聊天
--stream / --no-stream   强制开/关流式输出
--version           版本号
```

## 交互命令

| 命令 | 作用 |
|---|---|
| `/status` | 查看当前配置 |
| `/reset` | 清空对话历史 |
| `/tools` | 列出可用工具 |
| `/exit` | 退出（`/quit`、`/q` 同义） |
| `Ctrl+C` ×3 | 连按三次退出；只按一次是取消当前任务 |

## 工具

| 工具 | 说明 |
|---|---|
| `shell` | 执行 shell 命令，返回 stdout/stderr/退出码；超时会杀掉整个进程树 |
| `read_file` | 分片读文件，每次最多 8KB，返回续读 offset |
| `write_file` | 写文件，支持覆盖/追加，父目录自动创建 |
| `list_dir` | 列目录，目录在前 |

## 安全策略

**普通命令直接执行，只有高危命令才请求确认。** 判定为高危的情况：

- 删除/权限/系统类命令：`rm`、`del`、`rmdir`、`chmod`、`sudo`、`reg`、`systemctl`、
  `shutdown`、`dd`、`mkfs`、`format`、`kill` 等，以及 PowerShell 的 `Remove-Item`、`Stop-Process`
- 改写历史：`git push --force`、`git reset --hard`、`git clean -f`、`git restore`
- 管道执行：`curl ... | sh`、`wget ... | bash`、`bash -c "..."` 里的内容
- 越界写入：`>` / `>>` 重定向到工作目录之外，或 `mv` 目标已存在/越界

文件工具的路径被限制在工作目录内（`--allow-outside` 可放开）。

> 注意：`shell` 本身不受路径沙箱约束，沙箱只管三个文件工具。高危识别是静态规则，
> `python -c "import shutil; shutil.rmtree('.')"` 这类藏在解释器里的调用判断不出来。
> 需要强隔离请用容器或降权运行。

## 其它机制

- **上下文裁剪**：超出预算时按「整轮丢弃 → 压缩工具结果 → 丢弃最旧工具往返块」三级处理，
  始终保证 `assistant/tool` 消息配对完整。
- **重试**：指数退避 + ±40% 随机抖动（1、2、4、8、16、32、60 秒封顶），
  优先采纳服务端返回的 `Retry-After`。429 默认给到 8 次机会。
- **编码**：子进程输出按 utf-8 → 系统编码 → gbk 依次尝试解码，中文 Windows 不乱码。

## 已知限制

- 只支持 `/chat/completions`，不兼容 `/responses` 接口
- 使用 `max_tokens` 字段，o1 / gpt-5 系等要求 `max_completion_tokens` 的模型需要自行调整
- 推理模型的 `reasoning_content` 不参与多轮回传

## License

[MIT](LICENSE) © 2026 style7en

可自由使用、修改、分发、商用，保留版权声明即可。完整条款见 [LICENSE](LICENSE)。
