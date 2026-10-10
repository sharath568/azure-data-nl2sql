-- nl2sql: least-privilege principals for Azure SQL Database.
-- Run as the server admin (or an Entra admin), connected to the *user* database,
-- not master. Contained users live in the database, so there is no server login.
--
--   python scripts/run_tsql.py "<admin conn>" sql/mssql/03_users_azure_sql.sql \
--       -v READER_PASSWORD='...' -v WEST_PASSWORD='...'
--
-- Recommended in production: no passwords at all. Give your app a managed identity
-- and create the user from Microsoft Entra ID instead (needs an Entra admin on the server):
--
--   CREATE USER [my-app-identity] FROM EXTERNAL PROVIDER;
--   ALTER ROLE nl2sql_readers ADD MEMBER [my-app-identity];   -- after 04_permissions.sql
--
-- and connect with Authentication=ActiveDirectoryDefault (mssql-python / ODBC Driver 18).

IF USER_ID(N'nl2sql_reader') IS NULL
    CREATE USER nl2sql_reader WITH PASSWORD = N'$(READER_PASSWORD)', DEFAULT_SCHEMA = dbo;
GO
IF USER_ID(N'nl2sql_reader_west') IS NULL
    CREATE USER nl2sql_reader_west WITH PASSWORD = N'$(WEST_PASSWORD)', DEFAULT_SCHEMA = dbo;
GO
