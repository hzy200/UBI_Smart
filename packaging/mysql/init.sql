-- A2 控制面数据库初始化（MySQL 8.0）。
--
--   mysql -u root -p < packaging/mysql/init.sql
--
-- 只授予该库所需的 DML + 迁移 DDL；不授予全局权限。生产环境建议把
-- “迁移账号”（CREATE/ALTER/INDEX）与“运行账号”（DML）拆开，桌面自动
-- 迁移场景可保持单账号。

CREATE DATABASE IF NOT EXISTS coding_agent
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'coding_agent'@'localhost'
  IDENTIFIED BY 'change-me';

GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES
  ON coding_agent.* TO 'coding_agent'@'localhost';

FLUSH PRIVILEGES;
