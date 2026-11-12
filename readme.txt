# 试验数据库

以内容寻址方式保存试验数据，登记类型化结构、数据版本与派生关系。发布审批指向固定版本；撤回来源会影响依赖成品的可交付性。

演练从本地合成试验数据完成校验、版本登记、派生和批准导出；导出包可独立核验。

主要代码位于 trialvault，按 artifacts、contracts、catalog、releases、design 划分职责；tests 保存领域行为、边界及存储回归用例。命令接收严格 JSON，详细操作名与字段可通过对应帮助入口及 service 中的注册表查询。示例中的机构、客户、案件和测量均为本地合成记录，不涉及真实个人资料。

前瞻性功效设计位于 design：版本化设计表登记处理组、区组随机分配、每批试片数与成本；批间/批内变异可取自指定历史数据版本（仅估计方差，不估处理效应）或显式假设并保留来源。最小有意义差异必须声明非历史来源。仿真按实际分配单位生成数据并随机化，按匹配设计做批次级或分层检验，零效应场景核验一类错误，报告蒙特卡洛标准误与 Wilson 区间；整批缺失与单片缺失分开计数，失败拟合、不可估及完成轮次全部保留。固定预算下比较候选设计的成本、功效与稳健性并给出非支配前沿，批内相关与失访率敏感性单独列出。相同配置与种子可逐轮复算（design_verify），输入版本撤回后设计、运行与比较均标记 planning_invalid。该步骤不读取未来试验结果，也不产生正式疗效结论。

使用 Python 3.11.2 与标准库，无第三方包。运行无需访问外部服务或提供账号。依赖清单 requirements.txt 为空，.python-version 固定解释器版本。

在项目根目录运行：

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q trialvault
python3 -m trialvault demo
```

前瞻性设计可直接调用 `Vault.designs`（`define`、`variance`、`plan`、`verify`、`compare`），对应 CLI 动作为 `design_define`、`design_get`、`designs`、`design_variance`、`design_plan`、`design_run`、`design_runs`、`design_verify`、`design_compare`、`design_comparison`。设计表、仿真请求与结果均写入目录数据库并带摘要，运行写入不影响既有版本、发布与导出流程。

也可执行下面的统一检查，按顺序进行语法编译、领域回归和完整本地演练，任一环节失败即退出。运行写入均在临时目录或内存数据库，重复执行不需清理已有业务账。正常数据规模的检查可在 90 秒内完成。

```sh
sh scripts/check.sh
```
