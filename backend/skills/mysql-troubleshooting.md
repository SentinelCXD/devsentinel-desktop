---
name: mysql-troubleshooting
description: MySQL 故障排查手册：连接池耗尽、慢查询、死锁与锁等待，含定位命令与常见根因
---
# MySQL 排查手册

适用症状：请求超时 / 大面积 503、接口变慢、死锁报错、连接数暴涨。

## 1. 连接池耗尽（最常见）

症状：大量请求超时或 503；应用日志出现 `acquire connection timeout`、
`pool exhausted`、`too many connections`。

定位顺序：
1. 应用日志搜关键词：timeout、pool、connection、exhausted
2. 代码搜连接池配置：POOL_MAX、pool_size、max_connections、minconn
3. 数据库侧：`SHOW STATUS LIKE 'Threads_connected';`（对比 max_connections）
4. 查近期变更：池参数类提交（git log 里找 pool / config / memory 字样）

常见根因：
- 池上限被调小（多为"优化内存"类提交误伤，回滚即可恢复）
- 连接泄漏：借出不还（缺 finally / with，看代码里 acquire 后的异常路径）
- 慢查询长期占用连接不放（转第 2 节）

修复：恢复池大小；泄漏处补释放逻辑；慢 SQL 治理。
验证：压测下 `Threads_connected` 稳定低于池上限，超时日志消失。

## 2. 慢查询

症状：特定接口变慢；慢查询日志增长；`Threads_running` 偏高。

定位：
1. `SHOW FULL PROCESSLIST;` 看当前在跑什么，State 列找耗时状态（如 Sending data / Waiting for lock；状态名随 MySQL 版本不同，以实际输出为准）
2. 开慢日志：`SET GLOBAL slow_query_log = ON;` + `long_query_time`
3. `EXPLAIN` 可疑 SQL，重点看 type=ALL（全表扫描）、rows 估算、缺索引

常见根因：缺索引；大事务；`SELECT *` 拖大字段；统计信息过期。

## 3. 死锁与锁等待

症状：`Deadlock found when trying to get lock`；事务挂起不返回。

定位：
1. `SHOW ENGINE INNODB STATUS;` 看 LATEST DETECTED DEADLOCK 段
   ——两个事务各持有什么锁、各在等什么
2. 找到两条 SQL 后回代码定位事务顺序（多半是两张表 A→B / B→A 交叉加锁）

常见根因：事务内多表更新顺序不一致；事务里夹 RPC/慢 IO 拉长持锁时间；
唯一索引冲突重试。

修复：统一加锁顺序；事务瘦身（IO 移出事务）；必要时降低隔离级别。

## 4. 排查命令速查

- 连接：`SHOW STATUS LIKE 'Threads%';` / `SHOW PROCESSLIST;`
- 锁：`SHOW ENGINE INNODB STATUS;` / `SELECT * FROM information_schema.INNODB_TRX;`
- 慢查询：`SHOW VARIABLES LIKE 'slow%';`
- 表状态：`SHOW TABLE STATUS;` / `ANALYZE TABLE 表名;`

## 5. 用 DevSentinel 工具怎么查

1. read_log：keywords 传 ["timeout", "pool", "connection"]（不确定服务就留空全局搜）
2. search_code：搜 POOL_MAX / pool_size / max_connections
3. git_log → git_show：看池参数是否被近期提交改小
