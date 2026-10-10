-- nl2sql: least-privilege principals for SQL Server (box, VM or container).
-- For Azure SQL Database use 03_users_azure_sql.sql instead.
--
-- Run as an admin, connected to the sample database (nl2sql_sample):
--   python scripts/run_tsql.py "<admin conn>" sql/mssql/03_login_sqlserver.sql \
--       -v READER_PASSWORD='...' -v WEST_PASSWORD='...'
--
-- Two principals:
--   nl2sql_reader       what nl2sql connects as
--   nl2sql_reader_west  same rights, but row-level security (05_*.sql) limits it to West customers
--
-- They get no permissions here. 04_permissions.sql grants exactly what they need.

IF SUSER_ID(N'nl2sql_reader') IS NULL
    CREATE LOGIN nl2sql_reader WITH PASSWORD = N'$(READER_PASSWORD)', CHECK_POLICY = ON;
GO
IF SUSER_ID(N'nl2sql_reader_west') IS NULL
    CREATE LOGIN nl2sql_reader_west WITH PASSWORD = N'$(WEST_PASSWORD)', CHECK_POLICY = ON;
GO
IF USER_ID(N'nl2sql_reader') IS NULL
    CREATE USER nl2sql_reader FOR LOGIN nl2sql_reader WITH DEFAULT_SCHEMA = dbo;
GO
IF USER_ID(N'nl2sql_reader_west') IS NULL
    CREATE USER nl2sql_reader_west FOR LOGIN nl2sql_reader_west WITH DEFAULT_SCHEMA = dbo;
GO
