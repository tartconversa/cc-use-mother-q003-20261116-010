# LoopLake 回收物料数据台

这是面向社区回收运营分析员的本地数据湖与数仓服务。它将智能回收箱投递、地磅入出站、转运和兑付事件保存为不可改写的原始记录，并生成带有效期维度、物料对账、付款校验和冻结发布的街道月度视图。所有示例均为合成数据。

## 使用

运行环境为 Python 3.11+；依赖版本见 `requirements.txt`。测试：`python -m unittest discover -s tests -v`；语法检查：`python -m compileall -q looplake`；完整本地演示：`python -m looplake.demo`。HTTP 服务可运行 `uvicorn looplake.api:app`，并在 `/docs` 查阅接口；CLI 为 `python -m looplake.cli --help`。设置 `LOOPLAKE_DB` 可改变 API SQLite 路径。

先登记来源和带有效期的街道、社区、设备、物料、站点与人员维度，再按来源导入事件；同一来源和外部 ID 的不同正文会拒绝，完全重复事件幂等返回。维度未就绪的输入进入隔离状态，登记维度后可按 ingestion ID 重试。批量导入支持清单指纹和检查点，防止使用不同清单误续跑。

同一物料批次的投递与地磅入站重量需在容差内相符；不同物料、出站超入站、付款与费率不符均产生可解释的差异。街道月度报表仅纳入已解决批次，不会将投递、地磅与转运三条记录相加。发布前读取预览指纹；发布时再次校验数据与不可变名称。冻结版保留聚合、对账和事实血缘，日后的新批次不会修改旧版。

## 投递撤销与付款调整

误投投递不删除，而是用 `revocation` 事件撤销：它经 `target_external_id`（默认同来源，可用 `target_source` 指定）引用原投递，归一化后把原投递事实标记为 `revoked`，并记录原业务日期 `original_day` 与处理日期 `day`。已兑付的环保金用 `payout_adjustment` 事件冲回：它引用对应的撤销事件，携带带符号的 `adjustment_yuan`（追偿为负、补付为正）、收款人 `actor_id` 与原批次 `batch_id`，落在处理月份而不是倒改已发布月结。`GET /batches/{batch_id}/payments`（CLI `payments`）按事件顺序给出兑付、撤销、调整流水，含原始业务日期、处理日期、收款人、目标事实链与滚动净付额。

同一撤销或调整重复进入按来源与外部 ID 幂等返回，不会多扣钱；内容不同的同 ID 事件拒绝。撤销目标未就绪（投递未到、已被撤销、设备/批次/物料不符、时间倒置）或调整引用的撤销未到、收款人与原兑付不符时，事件进入隔离待核，登记或重试后可恢复。应付按未撤销投递计价，实付为兑付加调整流水：部分追偿产生 `clawback_outstanding`、超额追偿产生 `clawback_overpaid`，均令批次待核并阻止发布，直至净额归零。月度集市新增 `revoked_kg` 与 `adjustment_yuan` 列，跨月冲回在处理月列示，冻结发布保持原貌。

## 输入约定

时间带时区，重量用 kg，金额用元；业务日期由事件时间换算为 UTC 后记录。维度的 valid_to 为开区间。来源 channel 与事件 channel 必须一致。演示代码 `looplake/demo.py` 展示完整输入和可观察输出。HTTP 错误返回 `code` 和 `detail`；422 表示输入形状不合法，409 表示版本或业务冲突。撤销事件的 channel 为 smart_box 且需 device_id；调整事件的 channel 为 payment 且需 actor_id、`target_source` 与非零 `adjustment_yuan`。
