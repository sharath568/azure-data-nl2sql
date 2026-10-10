-- nl2sql: what the read-only principals may do. Run after 03_*.sql, as an admin,
-- in the sample database. Safe to re-run.
--
-- This is the real security boundary. The guardrails in nl2sql inspect SQL text
-- and can have bugs; these permissions are enforced by the engine on every
-- statement, whatever text reaches it.
--
-- Why a custom role and not db_datareader: db_datareader can read every table in
-- every schema, including ones added next year. GRANT SELECT ON SCHEMA::dbo is
-- the same idea, scoped to one schema, and the column DENYs below sit on top.

IF DATABASE_PRINCIPAL_ID(N'nl2sql_readers') IS NULL
    CREATE ROLE nl2sql_readers AUTHORIZATION dbo;
GO

-- Read the application schema. Nothing else.
GRANT SELECT ON SCHEMA::dbo TO nl2sql_readers;

-- Sensitive columns: denied at the column level. A DENY wins over the schema GRANT,
-- so SELECT salary, WHERE salary > ..., ORDER BY ssn and SELECT * on these
-- tables all fail with error 230, even if a guardrail were bypassed.
DENY SELECT ON dbo.employees (salary, ssn) TO nl2sql_readers;
DENY SELECT ON dbo.customers (email) TO nl2sql_readers;

-- Belt and braces: explicit DENY on writes and code execution. If someone later
-- adds these users to db_datawriter or db_owner by mistake, DENY still wins
-- (except for db_owner/sysadmin, who bypass permission checks: never use them here).
DENY INSERT, UPDATE, DELETE, ALTER, EXECUTE, REFERENCES ON SCHEMA::dbo TO nl2sql_readers;
DENY CREATE TABLE, CREATE VIEW, CREATE PROCEDURE, CREATE FUNCTION TO nl2sql_readers;

-- Lets nl2sql ask the optimizer for an estimate (SET SHOWPLAN_XML) before running a query.
-- Trade-off: plans can reveal statistics and parameter values. Drop this grant and set
-- policy.cost.use_plan_estimates: false if that matters more to you than the cost guard.
GRANT SHOWPLAN TO nl2sql_readers;
GO

IF USER_ID(N'nl2sql_reader') IS NOT NULL
    ALTER ROLE nl2sql_readers ADD MEMBER nl2sql_reader;
IF USER_ID(N'nl2sql_reader_west') IS NOT NULL
    ALTER ROLE nl2sql_readers ADD MEMBER nl2sql_reader_west;
GO
