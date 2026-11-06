# 验收样例

`customers.csv`、`customers.xlsx`、`customers.parquet` 含相同的 6 行客户样例，可在页面逐一上传。CSV 为 UTF-8；XLSX / Parquet 从同一 CSV 通过固定引擎依赖生成。

按 `rules.json` 的顺序配置：去首尾空格 → 邮箱小写 → 城市缺失填 Unknown → id 去重 → 年龄转整数 → 年龄范围 0–150 → 邮箱完整正则匹配。

预期结果：3 行有效数据（id=1、2、5）、3 行脏数据（重复 id=2、非法邮箱 id=3、年龄越界 id=4）。结果名称 Alice 的空白被移除，邮箱小写，Bob 的城市补为 Unknown。脏数据保留输入的原始值及首次拒绝原因。报告逐规则统计、结果 CSV / XLSX / Parquet 和脏数据 CSV 可通过任务详情下载。

`scripts/smoke_test.py` 自动运行上述流程，另外检查三种输出重新导入、规则版本、权限和多分片上传。
