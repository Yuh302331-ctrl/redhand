# 文件改动记录

这是 redhand 和 agenttrueup 的桌面界面，支持 macOS、Windows 和 Linux。应用自带 Python 运行环境，本地记录、预览、恢复，以及核对选定的 AI 对话日志。

打开应用，选「试用演示」查看自带文本的前后对比，或选一个文件夹开始记录。关闭应用会停止监听。快照保存在系统的本机应用数据目录。

## 构建

每个包在对应系统和架构上构建。需要装有 PyInstaller 的 Python 3.12 环境，以及 `agenttrueup` 源码。Mac 还需要 macOS 14+ 和 Xcode 命令行工具。Linux 打包测试需要显示会话或 `xvfb-run`。

```bash
python desktop/package.py /path/to/agenttrueup ./desktop-work ./release-assets
```

GitHub Desktop packages 工作流为每个发行目标构建、验证并归档。macOS 预览包使用临时签名，Windows 预览包使用未签名的可执行文件。企业管理的设备可能要求发布者签名。
