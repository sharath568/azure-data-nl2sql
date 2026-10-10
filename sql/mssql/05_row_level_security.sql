-- nl2sql: row-level security example. Run after 04_permissions.sql, as an admin.
--
-- nl2sql_reader_west only sees customers in the West region. Every other user
-- (including nl2sql_reader) sees all customers. The filter is applied by the
-- engine to every query, so a generated query cannot opt out of it.
--
-- The user-to-region map lives in a separate `security` schema that the readers
-- cannot read (they only have SELECT on dbo).
--
-- Only dbo.customers is filtered here, to keep the example small. In a real
-- system you would filter orders too (through customer_id), or every query that
-- starts from orders would still see other regions' rows.

IF SCHEMA_ID(N'security') IS NULL
    EXEC (N'CREATE SCHEMA security AUTHORIZATION dbo');
GO

DROP SECURITY POLICY IF EXISTS security.customer_region_policy;
DROP FUNCTION IF EXISTS security.fn_customer_region;
DROP TABLE IF EXISTS security.reader_regions;
GO

CREATE TABLE security.reader_regions (
    user_name sysname NOT NULL PRIMARY KEY,
    region NVARCHAR(20) NOT NULL
);
INSERT INTO security.reader_regions (user_name, region) VALUES (N'nl2sql_reader_west', N'West');
GO

CREATE FUNCTION security.fn_customer_region (@region NVARCHAR(20))
RETURNS TABLE
WITH SCHEMABINDING
AS
RETURN
    SELECT 1 AS allowed
    WHERE NOT EXISTS (SELECT 1 FROM security.reader_regions AS r WHERE r.user_name = USER_NAME())
       OR EXISTS (
            SELECT 1 FROM security.reader_regions AS r
            WHERE r.user_name = USER_NAME() AND r.region = @region
       );
GO

CREATE SECURITY POLICY security.customer_region_policy
    ADD FILTER PREDICATE security.fn_customer_region(region) ON dbo.customers
    WITH (STATE = ON);
GO
