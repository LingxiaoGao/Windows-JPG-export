# 构建记录

完整实现位于 portable-exporter-v3 分支。云端工作流执行源码测试、PyInstaller onedir 打包、冻结 EXE 测试、GUI 冒烟检查和 SHA-256 生成。

本地首轮 EXE 测试发现 ExifTool 异常，未发布失败产物。此分支加入 Windows 子进程 DLL 环境隔离，使用固定 SHA-256 的完整 ExifTool Windows 分发包重新验证。

真实 Sony A1/A1 II 样片、干净 Windows 10 虚拟机及长期压力测试尚未提供或执行；不声称这些项目通过。
