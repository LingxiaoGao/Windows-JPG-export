# 原脚本审计与竖拍分析

审计输入为用户本机 `safari_arw_to_jpg.py` v1.0；与参考对话的 `safari_arw_to_jpg(1).py` 内容一致。SHA-256：`34ff83463af7c519330585b2732754d4a850126bea21ce470dd81358080884ee`。原文件保留在源码 `resources/original_safari_arw_to_jpg_v1.py`，仅作审计参考，不由软件导入或打包运行。

## 实际回退条件

原脚本从 `JpgFromRaw`、`PreviewImage` 中选择有效 JPEG，以像素面积最大的候选为最佳。函数参数 `target` 在选择候选时没有用于过滤。

只有当 `payload is not None and max(ew, eh) >= target` 时使用内嵌图；其余情况调用 `open_rendered_raw()` / rawpy。默认目标长边为 3072px。

**代码没有按横拍、竖拍或长宽比单独触发回退。** 90 度方向交换不改变 `max(width,height)`，仅修复 EXIF 方向不能让 1616px 的内嵌图变成 3072px。用户观察的竖拍回退可能来自真实内嵌尺寸、标签可用性、候选完整性或机型/设置差异；目前没有提供对应真实 ARW，不能确定具体原因。

原 `ImageOps.exif_transpose` 只使用内嵌 JPEG 的方向，未读取 ARW 外层方向。JPEG 没带方向时可能显示横着，但这与上述分辨率回退是独立问题。

## 新版修正

- 沿用并扩展原脚本的 ExifTool 提取、完整 JPEG 检查、方向处理、原子输出和 SQLite/CSV 思路。`embedded_legacy.py` 是经移除 RAW 解码的 v2 中间版；`export_core.py` 和 GUI 扩展为产品。
- 检查 `JpgFromRaw`、`PreviewImage`、`OtherImage`、`ThumbnailImage`；逐个确认确实是有效 JPEG。像素面积最高者优先，面积相同长边优先，再按固定标签顺序。单标签缺失不会判定整个 ARW 无 JPEG。
- 无 rawpy、LibRaw 或 NumPy 运行依赖；打包明确排除这些模块。
- JPEG 与 ARW EXIF Orientation 一致或能补齐时转正像素。矛盾/尺寸歧义会报错，默认不凭宽大于高判断拍摄方向；正常方向缺失会记录明确假定。
- JPEG 标记 1 且几何尺寸已相对 RAW 交换时，沿用已转正矩阵，清单记尺寸判断来源。特殊裁切和编辑过元数据可能使此判断不可靠，需真实样片检查。
- 最终产品按用户新规则：低于目标时保留原尺寸并正常完成、标记，不再因不足分辨率失败或调用 RAW 解码。

## v1 与产品差异

| 项目 | 原 v1 | 便携 v3 |
|---|---|---|
| 日常操作 | Python 命令行 | 中文 Windows GUI，拖文件夹 |
| 输入 | ARW | ARW/JPG/JPEG 混合 |
| RAW 回退 | 可调用 rawpy | 永久禁止 |
| 默认分享 | 3072px、质量 85 | 2560px、95→最低 88、正常 4MB 上限 |
| 小内嵌图 | 转 RAW 解码 | 保留尺寸、标记 |
| 原样 | 可额外保存 | 独立预设，字节校验 |
| 拍摄 EXIF | 分享图不保留 | 白名单保留，强制清理定位数据 |
| 续跑 | 大小/mtime/参数/存在 | 同时校验输出 SHA-256 |
| 同名混合输入 | 未支持 | 稳定后缀/哈希，不覆盖 |
| 测试 | 原脚本未附测试 | 源码及冻结 EXE 自动验收 |

## 未经验证的部分

Sony A1、A1 II、曾触发 rawpy 的竖拍、高速连拍、真实 Lightroom/Capture One 导出样片都未提供。程序预检及清单会记录每张实际文件的标签、尺寸、方向和颜色来源；这些数据能用于进一步定位，不能由本次合成测试推断所有相机均兼容。
