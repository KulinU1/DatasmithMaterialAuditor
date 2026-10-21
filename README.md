# Datasmith Material Auditor

版本：0.9.6

面向 **Unreal Engine 5.8 / Windows x64** 的中文编辑器插件，用于 Datasmith 场景导入后的材质检查、盒体 UV 处理和模型质量审计。

## 功能

- 材质与缓冲检查：锁定选中的 Datasmith Root 范围，扫描材质、替换玻璃、标准化材质、检查双面与 Normal 输入，切换 World Normal / Scene Depth 预览。
- 一键盒体 UV：根据模型尺寸生成盒体 UV，支持批量执行、保存和报告。
- 模型质量检查（测试中）：通过 C++ 读取 Source LOD0 网格，检查退化面、法线、拓扑、UV 与镜像变换，并定位、高亮问题区域。

**0.9.6 已移除旧模型整理功能。** 大小、朝向、底部锚点与碰撞处理已移至独立的 SceneWeaver 工具；本仓库及安装包不包含 SceneWeaver、SetMeshTag 或 AssetPlacementPresets。

## 下载安装

普通用户请从 [Releases](https://github.com/Kulin886/DatasmithMaterialAuditor/releases) 下载带有 `UE5.8-Win64-WithQt` 的 ZIP。GitHub 的 **Source code** 压缩包只含源码，不包含 Qt 运行库和已编译 UE 模块。

1. 关闭使用该插件的 UE 编辑器。
2. 将 ZIP 中的 `DatasmithMaterialAuditor` 文件夹放到 `<项目>/Plugins/` 下，升级时完整替换旧插件。
3. 重启 UE，启用插件依赖，打开顶部菜单 **Datasmith 场景处理 → 打开 Datasmith 场景处理器**。

完整安装包包含 PySide6、PySide6_Essentials、PySide6_Addons、shiboken6 **6.11.1**、Qt DLL 和 Windows 平台插件，同事电脑无需另行安装 Python 包或运行 pip。

预编译模块针对 UE 5.8，打包使用的 Engine BuildId 为 `55116800`。如果引擎提示模块版本不同，请用对应引擎重新编译本仓库的 `Source`；不要直接修改模块的 BuildId 来绕过检查。

## 使用

在大纲中选择 Datasmith Root，打开面板后先点击 **读取并锁定当前选中 Root**，再执行材质扫描。修改材质、生成 UV 等操作会保存资产，执行前请在版本控制中保存可恢复版本。模型质量检查用于辅助定位问题，检查结果仍需人工复核，不能完全复现材质法线贴图或 GPU 渲染效果。

## 从源码构建

将仓库放到 C++ 项目的 `Plugins/DatasmithMaterialAuditor`，使用 UE 5.8 生成工程并编译 Editor 目标。插件声明了 PythonScriptPlugin、EditorScriptingUtilities、AutomationUtils 和 GeometryScripting 依赖。

源码仓库不追踪 Qt 二进制。可以从完整 Release 包复制 `Content/Python/Lib/Win64/site-packages`，或使用 **UE 内嵌 Python 3.11** 在仓库根目录执行：

```powershell
& '<UE_5.8>/Engine/Binaries/ThirdParty/Python3/Win64/python.exe' -m pip install --only-binary=:all: --no-compile --target Content/Python/Lib/Win64/site-packages -r requirements-qt.txt
```

UE 会自动注册此目录；无需修改系统 Python。Qt 版本应保持一致，不要混用不同版本的 PySide6 与 shiboken6。

## 验证

`tests/pure_*.py` 包含可独立运行的逻辑检查；其他测试需要在独立 UE 测试项目中运行。`tests/bundled_qt_smoke_test.py` 会排除外部 site-packages，确认 Qt 来自插件目录，并打开、关闭包含三个页签的面板。请勿在正式制作场景中运行会生成资产的测试。

## 许可

本项目自有源码采用 [MIT](LICENSE) 许可证，作者 Kulin。

Release 中的 Qt / PySide6 / shiboken6 及其第三方组件保留各自上游许可证，**不由本项目的 MIT 许可证重新授权**。详情见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)、`ThirdPartyLicenses` 和随包分发的 `*.dist-info` 文件。
## Star History

<a href="https://www.star-history.com/?repos=kulin886%2Fdatasmithmaterialauditor&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=kulin886/datasmithmaterialauditor&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=kulin886/datasmithmaterialauditor&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=kulin886/datasmithmaterialauditor&type=date&legend=top-left" />
 </picture>
</a>
