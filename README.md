# Windows RAW / JPG 高清导出器

中文、离线、Windows 10/11 x64、绿色便携 onedir ZIP。

三个预设：原始 JPEG、微信朋友圈 2560px、高质量分享 4096px。
ARW 仅提取内嵌 JPEG，禁止 RAW 解码；源照片严格只读。
小图保留尺寸，95→最低 88，4MB 正常上限允许例外并记录。
分享模式保留拍摄 EXIF、移除 GPS/XMP、进行正确 ICC 转换。

- [操作手册](README_中文.md)
- [原脚本审计](AUDIT_中文.md)
- [测试报告与限制](TEST_REPORT_中文.md)
- [依赖和许可](THIRD_PARTY_LICENSES.md)

GitHub Actions 在开发分支 `portable-exporter-v3` 更新时使用 Windows 2022 x64 runner 构建。
只有源码测试、冻结 EXE 测试、GUI 运行检查通过才发布便携 ZIP 产物。
v3.0.2 增加程序目录含中文/空格/emoji 的冻结测试、GUI 检查、内置运行时完整性校验及临时目录变化后的断点复用检查。
构建页面的 Artifacts 包含便携 ZIP 和 SHA-256。

开发者复现（用户日常无需执行）：Python 3.12 x64 安装
`requirements-build.txt` 后运行 `python build_release.py`。

尚未提供真实 Sony A1/A1 II ARW，相关机型测试未完成。
隔离 PATH 检查不等于干净虚拟机验证。未代码签名。
