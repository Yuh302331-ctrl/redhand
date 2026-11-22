# 文件改动记录

这是一个 macOS 桌面入口。文件记录和恢复复用 redhand，选择对话日志后在本机用 agenttrueup 核对。应用自带 Python 运行环境。

打开应用，选「试用演示」查看自带文本的前后对比，或选一个文件夹开始记录。关闭应用会停止监听。快照保存在 `~/Library/Application Support/File Change Desk`。

## 构建

需要 macOS 14 或更高版本、Xcode 命令行工具、装有 PyInstaller 的 Python 3.11+ 环境，以及同一工作区中的 agenttrueup 源码。构建以当前机器架构为目标，输出目录应为空。

```bash
bash desktop/build-mac.sh /path/to/agenttrueup /path/to/venv/bin/python /path/to/build /path/to/ChangeDesk.app
```

构建产物使用临时签名，供本机试用。面向其他 Mac 分发前需要开发者签名与公证。
