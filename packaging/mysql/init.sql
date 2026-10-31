-- UBISmart 控制面数据库（MySQL 8.0/8.4）
--
--   mysql -u root -p < packaging/mysql/init.sql
--
-- 生产建议：
--   1. 先把两个 change-me 密码换成随机值；
--   2. 迁移账号只给 DDL + DML，运行账号只给 DML；
--   3. 用迁移账号执行 `control_server --migrate` 或
--      `python -m ubismart.control.migrate`。
--
-- 表由 Alembic 创建，不在本文件里手写 CREATE TABLE，避免与迁移漂移。

CREATE DATABASE IF NOT EXISTS coding_agent
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

-- 运行账号：A2 只做业务读写，不能改表结构。
CREATE USER IF NOT EXISTS 'coding_agent'@'localhost'
  IDENTIFIED BY 'change-me-runtime';
GRANT SELECT, INSERT, UPDATE, DELETE
  ON coding_agent.* TO 'coding_agent'@'localhost';

-- 迁移账号：仅在发布/升级时使用。
CREATE USER IF NOT EXISTS 'coding_agent_migrator'@'localhost'
  IDENTIFIED BY 'change-me-migrator';
GRANT SELECT, INSERT, UPDATE, DELETE,
      CREATE, ALTER, INDEX, REFERENCES, DROP
  ON coding_agent.* TO 'coding_agent_migrator'@'localhost';

FLUSH PRIVILEGES;
