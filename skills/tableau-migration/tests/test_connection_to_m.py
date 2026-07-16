"""Tableau ``.tds`` parsing + M-emission tests (realistic XML fixtures)."""
import pytest

from connection_to_m import (
    build_m_field_resolver,
    combine_descriptors,
    connection_details_for_bind,
    custom_sql_parameter_refs,
    emit_connection_parameters,
    emit_m_partition_source,
    emit_table_tmdl_m,
    escape_m_string,
    extract_bundled_flatfile,
    m_partition_review_reason,
    parse_tds,
    tableau_type_to_tmdl,
)
from connection_to_m import _deescape_custom_sql
from connection_to_m import _odbc_connection_string, _scrub_odbc_extras

# -- fixtures (trimmed but structurally faithful .tds documents) ---------------
LIVE_SQLSERVER = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Superstore' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='myserver' name='sqlserver.0a1b2c'>
        <connection authentication='sqlserver' class='sqlserver' dbname='Superstore'
                    server='myserver.database.windows.net' username='svc' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.0a1b2c' name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Order ID</remote-name>
        <local-name>[Order ID]</local-name>
        <parent-name>[Orders]</parent-name>
        <local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name>
        <local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name>
        <local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Quantity</remote-name>
        <local-name>[Quantity]</local-name>
        <parent-name>[Orders]</parent-name>
        <local-type>integer</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Snowflake (case-sensitive backend): metadata-records keep the physical UPPERCASE name as the
# local-name (no friendly caption), so a calc's caption ([Sales]) resolves ONLY through the
# logical <column caption> + <cols> map layer. Physical REGION appears in two collection tables,
# disambiguated by caption (Region -> ORDERS, Region (People) -> PEOPLE). The calculated column
# carries a nested <calculation> and must be excluded from physical binding.
LIVE_SNOWFLAKE_LOGICAL = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Snowflake-Superstore' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='snow' name='snowflake.12zi'>
        <connection class='snowflake' dbname='TABLEAUCONNECT'
                    server='x.snowflakecomputing.com' warehouse='' />
      </named-connection>
    </named-connections>
    <relation type='collection'>
      <relation connection='snowflake.12zi' name='ORDERS' table='[TABLEAUCONNECT].[PUBLIC].[ORDERS]' type='table' />
      <relation connection='snowflake.12zi' name='PEOPLE' table='[TABLEAUCONNECT].[PUBLIC].[PEOPLE]' type='table' />
    </relation>
    <cols>
      <map key='[SALES]' value='[ORDERS].[SALES]' />
      <map key='[REGION]' value='[ORDERS].[REGION]' />
      <map key='[STATE]' value='[ORDERS].[STATE]' />
      <map key='[REGION (PEOPLE)]' value='[PEOPLE].[REGION]' />
    </cols>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>SALES</remote-name>
        <local-name>[SALES]</local-name>
        <parent-name>[ORDERS]</parent-name>
        <local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>REGION</remote-name>
        <local-name>[REGION]</local-name>
        <parent-name>[ORDERS]</parent-name>
        <local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>STATE</remote-name>
        <local-name>[STATE]</local-name>
        <parent-name>[ORDERS]</parent-name>
        <local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>REGION</remote-name>
        <local-name>[REGION]</local-name>
        <parent-name>[PEOPLE]</parent-name>
        <local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <column caption='Sales' datatype='real' name='[SALES]' role='measure' type='quantitative' />
  <column caption='Region' datatype='string' name='[REGION]' role='dimension' type='nominal' />
  <column caption='State' datatype='string' name='[STATE]' role='dimension' type='nominal' />
  <column caption='Region (People)' datatype='string' name='[REGION (PEOPLE)]' role='dimension' type='nominal' />
  <column caption='Profit Ratio' datatype='real' name='[Calculation_123]' role='measure' type='quantitative'>
    <calculation class='tableau' formula='SUM([Profit])/SUM([Sales])' />
  </column>
</datasource>"""

EXTRACT_OVER_SQLSERVER = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='SuperstoreExtract' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='srv' name='sqlserver.x'>
        <connection class='sqlserver' dbname='Superstore' server='srv.example.com' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.x' name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <extract enabled='true'>
    <connection class='hyper' dbname='Data/Datasources/Superstore.hyper' />
  </extract>
</datasource>"""

# A modern object-model extract .tds as Tableau Server materializes it: the live/logical relations
# AND a parallel `[Extract].[...]_HASH` cache layer (plus an <object-graph> that pairs each live
# relation with its cache twin). Authored fixture (own catalog/schema/table names) -- structurally
# faithful to the shape but not a reproduction of any real customer .tds. The cache twins must be
# dropped in favour of the live relations (else a DirectLake rebuild binds to a non-existent entity).
EXTRACT_OBJECT_MODEL = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='SalesExtract' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='dbx' name='databricks.abc'>
        <connection class='databricks' dbname='salesdb' server='adb-x.azuredatabricks.net' />
      </named-connection>
    </named-connections>
    <relation type='collection'>
      <relation connection='databricks.abc' name='sales' table='[salesdb].[core].[sales]' type='table' />
      <relation connection='databricks.abc' name='people' table='[salesdb].[core].[people]' type='table' />
    </relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Amount</remote-name><local-name>[Amount]</local-name>
        <parent-name>[sales]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[people]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <extract enabled='true'>
    <connection class='hyper' dbname='Data/extract.hyper'>
      <relation type='collection'>
        <relation name='sales (salesdb.core.sales)_AB12' table='[Extract].[sales (salesdb.core.sales)_AB12]' type='table' />
        <relation name='people (salesdb.core.people)_CD34' table='[Extract].[people (salesdb.core.people)_CD34]' type='table' />
      </relation>
    </connection>
  </extract>
  <object-graph>
    <objects>
      <object caption='sales' id='sales (salesdb.core.sales)_AB12'>
        <properties>
          <relation connection='databricks.abc' name='sales' table='[salesdb].[core].[sales]' type='table' />
          <relation name='sales (salesdb.core.sales)_AB12' table='[Extract].[sales (salesdb.core.sales)_AB12]' type='table' />
        </properties>
      </object>
      <object caption='people' id='people (salesdb.core.people)_CD34'>
        <properties>
          <relation connection='databricks.abc' name='people' table='[salesdb].[core].[people]' type='table' />
          <relation name='people (salesdb.core.people)_CD34' table='[Extract].[people (salesdb.core.people)_CD34]' type='table' />
        </properties>
      </object>
    </objects>
  </object-graph>
</datasource>"""

# A legacy extract-ONLY .tds: the only relations live in the `[Extract]` namespace, with no live
# upstream relation to replace them. These MUST be kept -- they are the only tables the source has.
EXTRACT_ONLY = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='ExtractOnly' version='18.1'>
  <connection class='hyper' dbname='Data/extract.hyper'>
    <relation name='sales_AB12' table='[Extract].[sales_AB12]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Amount</remote-name><local-name>[Amount]</local-name>
        <parent-name>[sales_AB12]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

CUSTOM_SQL = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='CustomSQL' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='srv' name='sqlserver.y'>
        <connection class='sqlserver' dbname='Sales' server='srv.example.com' />
      </named-connection>
    </named-connections>
    <relation connection='sqlserver.y' name='Custom SQL Query' type='text'>SELECT "Region", SUM(Sales) AS Sales FROM Orders GROUP BY "Region"</relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[Custom SQL Query]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Custom SQL Query]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

GENERIC_ODBC_DRIVER = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='MinIO Lake' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='minio' name='genericodbc.abc'>
        <connection class='genericodbc' dbname='lake'
                    odbc-connect-string-extras='SSL=true;UID=admin;PWD=hunter2'
                    odbc-dbms-name='Trino' odbc-driver='Simba Trino ODBC Driver' odbc-dsn=''
                    port='8080' server='trino.minio.local' username='admin' password='hunter2' />
      </named-connection>
    </named-connections>
    <relation connection='genericodbc.abc' name='Custom SQL Query' type='text'>SELECT "region", SUM(sales) AS total FROM lake.orders GROUP BY "region"</relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>region</remote-name><local-name>[region]</local-name>
        <parent-name>[Custom SQL Query]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>total</remote-name><local-name>[total]</local-name>
        <parent-name>[Custom SQL Query]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

GENERIC_ODBC_DSN = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Bamboo' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='bamboo' name='genericodbc.dsn'>
        <connection class='genericodbc' odbc-dsn='Bamboo DSN'
                    odbc-driver='PostgreSQL Unicode(x64)' odbc-connect-string-extras='' />
      </named-connection>
    </named-connections>
    <relation connection='genericodbc.dsn' name='orders' table='[orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>region</remote-name><local-name>[region]</local-name>
        <parent-name>[orders]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

JOIN_TREE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Joined' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='srv' name='sqlserver.z'>
        <connection class='sqlserver' dbname='Sales' server='srv.example.com' />
      </named-connection>
    </named-connections>
    <relation join='inner' type='join'>
      <relation name='Orders' table='[dbo].[Orders]' type='table' />
      <relation name='People' table='[dbo].[People]' type='table' />
      <clause type='join'><expression op='='></expression></clause>
    </relation>
  </connection>
</datasource>"""

SNOWFLAKE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Snow' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='acct' name='snowflake.a'>
        <connection class='snowflake' dbname='ANALYTICS' server='acct.snowflakecomputing.com'
                    warehouse='COMPUTE_WH' />
      </named-connection>
    </named-connections>
    <relation name='ORDERS' table='[PUBLIC].[ORDERS]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>SALES</remote-name><local-name>[SALES]</local-name>
        <parent-name>[ORDERS]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# A physical join tree WITH real equality clauses, columns, and a role-playing alias (`Manager`
# over the physical [Customer] table). Exercises leaf-table surfacing, physical-join relationship
# recovery from the join <clause> predicates, and alias distinctness -- the shape a real
# Salesforce / SQL-Server join datasource uses.
PHYSICAL_JOIN_KEYS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Joined' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='srv' name='sqlserver.z'>
        <connection class='sqlserver' dbname='Sales' server='srv.example.com' />
      </named-connection>
    </named-connections>
    <relation join='inner' type='join'>
      <relation join='left' type='join'>
        <relation name='Orders' table='[dbo].[Orders]' type='table' />
        <relation name='Customer' table='[dbo].[Customer]' type='table' />
        <clause type='join'>
          <expression op='='>
            <expression op='[Orders].[CustomerId]' />
            <expression op='[Customer].[Id]' />
          </expression>
        </clause>
      </relation>
      <relation name='Manager' table='[dbo].[Customer]' type='table' />
      <clause type='join'>
        <expression op='='>
          <expression op='[Orders].[ManagerId]' />
          <expression op='[Manager].[Id]' />
        </expression>
      </clause>
    </relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Id</remote-name><local-name>[Id]</local-name>
        <parent-name>[Orders]</parent-name><local-type>integer</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>CustomerId</remote-name><local-name>[CustomerId]</local-name>
        <parent-name>[Orders]</parent-name><local-type>integer</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>ManagerId</remote-name><local-name>[ManagerId]</local-name>
        <parent-name>[Orders]</parent-name><local-type>integer</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Id</remote-name><local-name>[Id]</local-name>
        <parent-name>[Customer]</parent-name><local-type>integer</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Name</remote-name><local-name>[Name]</local-name>
        <parent-name>[Customer]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

ORACLE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Ora' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='ora' name='oracle.a'>
        <connection class='oracle' server='oradb.example.com:1521/ORCL' username='app' />
      </named-connection>
    </named-connections>
    <relation name='ORDERS' table='[SALES].[ORDERS]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>ORDER_ID</remote-name><local-name>[ORDER_ID]</local-name>
        <parent-name>[ORDERS]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>SALES</remote-name><local-name>[SALES]</local-name>
        <parent-name>[ORDERS]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

TERADATA = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='TD' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='td' name='teradata.a'>
        <connection class='teradata' dbname='ANALYTICS' server='td.example.com' />
      </named-connection>
    </named-connections>
    <relation name='ORDERS' table='[ANALYTICS].[ORDERS]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>SALES</remote-name><local-name>[SALES]</local-name>
        <parent-name>[ORDERS]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Azure Synapse Analytics (Tableau class 'azure_sql_dw') speaks the SQL Server TDS protocol, so
# it binds through Sql.Database exactly like sqlserver / azure_sqldb.
SYNAPSE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Syn' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='syn' name='azure_sql_dw.a'>
        <connection class='azure_sql_dw' dbname='WideWorld' server='syn.sql.azuresynapse.net' />
      </named-connection>
    </named-connections>
    <relation name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Databricks: host + SQL-warehouse HTTP path, Unity Catalog catalog in dbname, [schema].[table].
DATABRICKS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Dbx' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='dbx' name='databricks.a'>
        <connection class='databricks' dbname='main' server='adb-123.azuredatabricks.net'
                    http-path='/sql/1.0/warehouses/abc123' />
      </named-connection>
    </named-connections>
    <relation name='ORDERS' table='[sales].[orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>amount</remote-name><local-name>[amount]</local-name>
        <parent-name>[orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Databricks custom SQL (native query): the real-world gap -- a `type='text'` relation with an
# embedded SQL string, true source column names carrying spaces/slashes, on a Unity-catalog host.
DATABRICKS_CUSTOM_SQL = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='DbxSQL' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='dbx' name='databricks.a'>
        <connection class='databricks' dbname='tableau_migration_databricks'
                    server='adb-123.azuredatabricks.net'
                    http-path='/sql/1.0/warehouses/240f0d0d01d9e8dd' />
      </named-connection>
    </named-connections>
    <relation connection='databricks.a' name='Custom SQL Query' type='text'>SELECT o.`Order ID`, o.`Country/Region`, o.Sales FROM orders o</relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Order ID</remote-name><local-name>[Order ID]</local-name>
        <parent-name>[Custom SQL Query]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Country/Region</remote-name><local-name>[Country/Region]</local-name>
        <parent-name>[Custom SQL Query]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Custom SQL Query]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Databricks custom SQL exactly as Tableau SERIALIZES it: every '<'/'>' in the query text is
# doubled on save (a blind global substitution that also hits comments + string literals) and
# halved back on read. Stored in a CDATA block (Tableau wraps the relation in CDATA whenever the
# text contains angle brackets). The extractor must reverse the doubling so the emitted query is
# the single-operator form the source can actually run.
DATABRICKS_CUSTOM_SQL_DOUBLED = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='DbxSQL' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='dbx' name='databricks.a'>
        <connection class='databricks' dbname='tableau_migration_databricks'
                    server='adb-123.azuredatabricks.net'
                    http-path='/sql/1.0/warehouses/240f0d0d01d9e8dd' />
      </named-connection>
    </named-connections>
    <relation connection='databricks.a' name='Custom SQL Query' type='text'><![CDATA[SELECT o.`Order ID`, o.Sales, o.Profit
FROM orders o
-- keep rows where Profit << 0 or Sales >> 1000
WHERE o.Profit << 0
   OR o.Sales >> 1000
   OR o.Quantity <<>> 1]]></relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Order ID</remote-name><local-name>[Order ID]</local-name>
        <parent-name>[Custom SQL Query]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Custom SQL Query]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Profit</remote-name><local-name>[Profit]</local-name>
        <parent-name>[Custom SQL Query]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Databricks custom SQL carrying a Tableau parameter reference, in the VERIFIED stored form: the
# reference is <[Parameters].[Name]> (single delimiters, bracketed 'Parameters') while the literal
# comparison operators around it are doubled. After de-escaping, the <[Parameters].[Threshold]>
# token survives -- it cannot yet be translated to a Power Query parameter, so the partition is
# still emitted but must be flagged needs_review.
DATABRICKS_CUSTOM_SQL_PARAM = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='DbxSQL' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='dbx' name='databricks.a'>
        <connection class='databricks' dbname='tableau_migration_databricks'
                    server='adb-123.azuredatabricks.net'
                    http-path='/sql/1.0/warehouses/240f0d0d01d9e8dd' />
      </named-connection>
    </named-connections>
    <relation connection='databricks.a' name='Custom SQL Query' type='text'><![CDATA[SELECT o.`Order ID`, o.Sales
FROM orders o
WHERE o.Profit >> <[Parameters].[Threshold]>
  AND o.Sales  << 5000]]></relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Order ID</remote-name><local-name>[Order ID]</local-name>
        <parent-name>[Custom SQL Query]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Custom SQL Query]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Snowflake custom SQL: shares the database_schema_table nav shape with Databricks, but is NOT in
# the live-verified NATIVE_QUERY_CATALOG_DRILL allow-list, so it must stay a flagged scaffold.
SNOWFLAKE_CUSTOM_SQL = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='SnowSQL' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='acct' name='snowflake.a'>
        <connection class='snowflake' dbname='ANALYTICS' server='acct.snowflakecomputing.com'
                    warehouse='COMPUTE_WH' />
      </named-connection>
    </named-connections>
    <relation connection='snowflake.a' name='Custom SQL Query' type='text'>SELECT "ORDER ID", SALES FROM ORDERS</relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>ORDER ID</remote-name><local-name>[ORDER ID]</local-name>
        <parent-name>[Custom SQL Query]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>SALES</remote-name><local-name>[SALES]</local-name>
        <parent-name>[Custom SQL Query]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Microsoft Fabric Warehouse / Lakehouse SQL endpoint (Tableau class
# 'microsoft_fabric_sql_endpoint'): a SQL Server TDS endpoint -> Sql.Database, like sqlserver.
FABRIC_SQL = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Fab' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='fab' name='fabric.a'>
        <connection class='microsoft_fabric_sql_endpoint' dbname='SalesWH'
                    server='abc.datawarehouse.fabric.microsoft.com' />
      </named-connection>
    </named-connections>
    <relation name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Azure SQL Managed Instance: Tableau reaches a Managed Instance through the ordinary SQL Server
# connector, so it arrives as connection class 'sqlserver' (the MI host just carries the
# instance-specific endpoint, often with a port). It must still bind through Sql.Database.
MANAGED_INSTANCE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Mi' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='mi' name='sqlserver.a'>
        <connection class='sqlserver' dbname='Sales'
                    server='myinst.public.0a1b2c3d4e5f.database.windows.net,3342' />
      </named-connection>
    </named-connections>
    <relation name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Azure Synapse Analytics serverless SQL pool: the Synapse connector emits the SAME class
# ('azure_sql_dw') for the serverless (on-demand) endpoint as for the dedicated pool, so a
# serverless workspace endpoint must bind through Sql.Database identically.
SYNAPSE_SERVERLESS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='SynS' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='syns' name='azure_sql_dw.b'>
        <connection class='azure_sql_dw' dbname='Lake'
                    server='myws-ondemand.sql.azuresynapse.net' />
      </named-connection>
    </named-connections>
    <relation name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Authored federated three-part-name collections (Tableau 2023+ object model). The same physical
# tables appear under a <relation type='collection'> AND again under a logical object-model layer;
# their columns live in <metadata-record class='column'> keyed by the bare relation name. These are
# original fixtures (own catalog/schema/table/column names), not reproductions of any live .tds.
SNOWFLAKE_COLLECTION = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Retail' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='sf' name='snowflake.x'>
        <connection authentication='Username Password' class='snowflake' dbname='RETAILDB'
                    schema='SALESM' server='myorg-acct.snowflakecomputing.com'
                    username='svc_loader' warehouse='' />
      </named-connection>
    </named-connections>
    <relation type='collection'>
      <relation connection='snowflake.x' name='INVOICE' table='[RETAILDB].[SALESM].[INVOICE]' type='table' />
      <relation connection='snowflake.x' name='CUSTOMER' table='[RETAILDB].[SALESM].[CUSTOMER]' type='table' />
    </relation>
    <object-model>
      <relation type='collection'>
        <relation connection='snowflake.x' name='INVOICE' table='[RETAILDB].[SALESM].[INVOICE]' type='table' />
        <relation connection='snowflake.x' name='CUSTOMER' table='[RETAILDB].[SALESM].[CUSTOMER]' type='table' />
      </relation>
    </object-model>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>INVOICE_NO</remote-name><local-name>[INVOICE_NO]</local-name>
        <parent-name>[INVOICE]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>AMOUNT</remote-name><local-name>[AMOUNT]</local-name>
        <parent-name>[INVOICE]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>CUSTOMER_NO</remote-name><local-name>[CUSTOMER_NO]</local-name>
        <parent-name>[CUSTOMER]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>TIER</remote-name><local-name>[TIER]</local-name>
        <parent-name>[CUSTOMER]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

DATABRICKS_COLLECTION = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Lake' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='dbx' name='databricks.y'>
        <connection authentication='oauth' class='databricks' dbname='lakehouse_cat'
                    instanceurl='https://adb-evt.example.net/oidc' oauth-config-id='oauth-secret-id'
                    schema='silver' server='adb-evt.example.net'
                    username='svc_admin@example.com' v-http-path='/sql/1.0/warehouses/cafe1234' />
      </named-connection>
    </named-connections>
    <relation type='collection'>
      <relation connection='databricks.y' name='WEB_EVENT' table='[lakehouse_cat].[silver].[WEB_EVENT]' type='table' />
      <relation connection='databricks.y' name='ACCOUNT' table='[lakehouse_cat].[silver].[ACCOUNT]' type='table' />
    </relation>
    <object-model>
      <relation type='collection'>
        <relation connection='databricks.y' name='WEB_EVENT' table='[lakehouse_cat].[silver].[WEB_EVENT]' type='table' />
        <relation connection='databricks.y' name='ACCOUNT' table='[lakehouse_cat].[silver].[ACCOUNT]' type='table' />
      </relation>
    </object-model>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>EVENT_ID</remote-name><local-name>[EVENT_ID]</local-name>
        <parent-name>[WEB_EVENT]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>EVENT_TS</remote-name><local-name>[EVENT_TS]</local-name>
        <parent-name>[WEB_EVENT]</parent-name><local-type>datetime</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>ACCOUNT_ID</remote-name><local-name>[ACCOUNT_ID]</local-name>
        <parent-name>[ACCOUNT]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>PLAN</remote-name><local-name>[PLAN]</local-name>
        <parent-name>[ACCOUNT]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# A single-connection federated star whose joins live in <object-graph><relationships>.
# Authored (not derived from any real .tds): tables SALE / REP / RMA, with one join key that
# carries a space ("Order Key") so the emitted relationship must reference the cleaned model
# identifier ("Order_Key"), and one renamed endpoint ("[REGION (REP)]") whose ' (REP)' caption
# suffix must be stripped before resolving against REP's columns.
FEDERATED_STAR = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Star' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='sf' name='snowflake.s'>
        <connection authentication='Username Password' class='snowflake' dbname='ANALYTICS'
                    schema='PUBLIC' server='acct.snowflakecomputing.com'
                    username='svc_loader' warehouse='WH1' />
      </named-connection>
    </named-connections>
    <relation type='collection'>
      <relation connection='snowflake.s' name='SALE' table='[ANALYTICS].[PUBLIC].[SALE]' type='table' />
      <relation connection='snowflake.s' name='REP' table='[ANALYTICS].[PUBLIC].[REP]' type='table' />
      <relation connection='snowflake.s' name='RMA' table='[ANALYTICS].[PUBLIC].[RMA]' type='table' />
    </relation>
    <object-graph>
      <objects>
        <object caption='SALE' id='SALE (ANALYTICS.SALE)_A1'>
          <properties>
            <relation connection='snowflake.s' name='SALE' table='[ANALYTICS].[PUBLIC].[SALE]' type='table' />
          </properties>
        </object>
        <object caption='REP' id='REP (ANALYTICS.REP)_B2'>
          <properties>
            <relation connection='snowflake.s' name='REP' table='[ANALYTICS].[PUBLIC].[REP]' type='table' />
          </properties>
        </object>
        <object caption='RMA' id='RMA (ANALYTICS.RMA)_C3'>
          <properties>
            <relation connection='snowflake.s' name='RMA' table='[ANALYTICS].[PUBLIC].[RMA]' type='table' />
          </properties>
        </object>
      </objects>
      <relationships>
        <relationship>
          <expression op='='>
            <expression op='[REGION]' />
            <expression op='[REGION (REP)]' />
          </expression>
          <first-end-point object-id='SALE (ANALYTICS.SALE)_A1' />
          <second-end-point object-id='REP (ANALYTICS.REP)_B2' />
        </relationship>
        <relationship>
          <expression op='='>
            <expression op='[Order Key]' />
            <expression op='[Order Key (RMA)]' />
          </expression>
          <first-end-point object-id='SALE (ANALYTICS.SALE)_A1' />
          <second-end-point object-id='RMA (ANALYTICS.RMA)_C3' />
        </relationship>
      </relationships>
    </object-graph>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Order Key</remote-name><local-name>[Order Key]</local-name>
        <parent-name>[SALE]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>REGION</remote-name><local-name>[REGION]</local-name>
        <parent-name>[SALE]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>AMT</remote-name><local-name>[AMT]</local-name>
        <parent-name>[SALE]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>REGION</remote-name><local-name>[REGION]</local-name>
        <parent-name>[REP]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>REP_NAME</remote-name><local-name>[REP_NAME]</local-name>
        <parent-name>[REP]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Order Key</remote-name><local-name>[Order Key]</local-name>
        <parent-name>[RMA]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>RMA_FLAG</remote-name><local-name>[RMA_FLAG]</local-name>
        <parent-name>[RMA]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Same object-graph shape, but with relationships that must each be skipped + recorded in
# relationship_warnings rather than emitted: one references a column absent from the endpoint
# table (stale/calculated join), one is a composite AND predicate (multi-column), and one is
# genuinely ambiguous (both join keys exist on both tables, so the orientation can't be trusted).
FEDERATED_REL_EDGECASE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Edge' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='sf' name='snowflake.s'>
        <connection authentication='Username Password' class='snowflake' dbname='ANALYTICS'
                    schema='PUBLIC' server='acct.snowflakecomputing.com' warehouse='WH1' />
      </named-connection>
    </named-connections>
    <relation type='collection'>
      <relation connection='snowflake.s' name='SALE' table='[ANALYTICS].[PUBLIC].[SALE]' type='table' />
      <relation connection='snowflake.s' name='REP' table='[ANALYTICS].[PUBLIC].[REP]' type='table' />
    </relation>
    <object-graph>
      <objects>
        <object caption='SALE' id='SALE (ANALYTICS.SALE)_A1'>
          <properties>
            <relation connection='snowflake.s' name='SALE' table='[ANALYTICS].[PUBLIC].[SALE]' type='table' />
          </properties>
        </object>
        <object caption='REP' id='REP (ANALYTICS.REP)_B2'>
          <properties>
            <relation connection='snowflake.s' name='REP' table='[ANALYTICS].[PUBLIC].[REP]' type='table' />
          </properties>
        </object>
      </objects>
      <relationships>
        <relationship>
          <expression op='='>
            <expression op='[REGION]' />
            <expression op='[REGION]' />
          </expression>
          <first-end-point object-id='SALE (ANALYTICS.SALE)_A1' />
          <second-end-point object-id='REP (ANALYTICS.REP)_B2' />
        </relationship>
        <relationship>
          <expression op='='>
            <expression op='[GHOST_KEY]' />
            <expression op='[REGION]' />
          </expression>
          <first-end-point object-id='SALE (ANALYTICS.SALE)_A1' />
          <second-end-point object-id='REP (ANALYTICS.REP)_B2' />
        </relationship>
        <relationship>
          <expression op='AND'>
            <expression op='='>
              <expression op='[REGION]' />
              <expression op='[REGION]' />
            </expression>
            <expression op='='>
              <expression op='[SEGMENT]' />
              <expression op='[SEGMENT]' />
            </expression>
          </expression>
          <first-end-point object-id='SALE (ANALYTICS.SALE)_A1' />
          <second-end-point object-id='REP (ANALYTICS.REP)_B2' />
        </relationship>
        <relationship>
          <expression op='='>
            <expression op='[REGION]' />
            <expression op='[SEGMENT]' />
          </expression>
          <first-end-point object-id='SALE (ANALYTICS.SALE)_A1' />
          <second-end-point object-id='REP (ANALYTICS.REP)_B2' />
        </relationship>
      </relationships>
    </object-graph>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>REGION</remote-name><local-name>[REGION]</local-name>
        <parent-name>[SALE]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>SEGMENT</remote-name><local-name>[SEGMENT]</local-name>
        <parent-name>[SALE]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>REGION</remote-name><local-name>[REGION]</local-name>
        <parent-name>[REP]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>SEGMENT</remote-name><local-name>[SEGMENT]</local-name>
        <parent-name>[REP]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Two named connections in one federated source (snowflake + sqlserver), each owning one table.
# Used to prove per-relation connector routing: emit must pick each relation's OWN connection
# class, not a single global one. (storage_mode still gates multi-connection to fallback; this
# only verifies the per-relation connector function + the exposed descriptor['connections'] map.)
MULTI_CONN = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Blend' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='sf' name='snowflake.s'>
        <connection authentication='Username Password' class='snowflake' dbname='ANALYTICS'
                    schema='PUBLIC' server='acct.snowflakecomputing.com' warehouse='WH1' />
      </named-connection>
      <named-connection caption='ss' name='sqlserver.t'>
        <connection authentication='SqlServer' class='sqlserver' dbname='Sales'
                    server='sql.example.com' />
      </named-connection>
    </named-connections>
    <relation type='collection'>
      <relation connection='snowflake.s' name='SALE' table='[ANALYTICS].[PUBLIC].[SALE]' type='table' />
      <relation connection='sqlserver.t' name='DimDate' table='[dbo].[DimDate]' type='table' />
    </relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>SALE_ID</remote-name><local-name>[SALE_ID]</local-name>
        <parent-name>[SALE]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>DateKey</remote-name><local-name>[DateKey]</local-name>
        <parent-name>[DimDate]</parent-name><local-type>integer</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Databricks federated source spelling the warehouse path as 'http-path' (older variant) rather
# than 'v-http-path', to confirm the attribute-name fallback resolves either spelling.
DATABRICKS_HTTPPATH_ALT = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Lake2' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='dbx' name='databricks.z'>
        <connection authentication='oauth' class='databricks' dbname='cat2'
                    http-path='/sql/1.0/warehouses/beef5678' schema='gold'
                    server='adb-2.example.net' />
      </named-connection>
    </named-connections>
    <relation type='collection'>
      <relation connection='databricks.z' name='FACT' table='[cat2].[gold].[FACT]' type='table' />
    </relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>FACT_ID</remote-name><local-name>[FACT_ID]</local-name>
        <parent-name>[FACT]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""

# Faithful reproduction of a modern multi-sheet Excel ``.tds`` (the published Superstore
# sample): a <relation type='collection'> container wrapping the physical sheet tables, the
# SAME tables duplicated under the logical <properties> layer, and columns in <metadata-records>.
EXCEL_COLLECTION = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Sample - Superstore' version='18.1'>
  <connection class='excel-direct' filename='Sample - Superstore.xlsx'>
    <relation type='collection'>
      <relation connection='excel-direct.0' name='Orders' table='[Orders$]' type='table' />
      <relation connection='excel-direct.0' name='People' table='[People$]' type='table' />
      <relation connection='excel-direct.0' name='Returns' table='[Returns$]' type='table' />
    </relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Row ID</remote-name><local-name>[Row ID]</local-name>
        <parent-name>[Orders$]</parent-name><local-type>integer</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders$]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Person</remote-name><local-name>[Person]</local-name>
        <parent-name>[People$]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[People$]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Returned</remote-name><local-name>[Returned]</local-name>
        <parent-name>[Returns$]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
    <objects>
      <object caption='Orders'><properties>
        <relation connection='excel-direct.0' name='Orders' table='[Orders$]' type='table' />
      </properties></object>
      <object caption='People'><properties>
        <relation connection='excel-direct.0' name='People' table='[People$]' type='table' />
      </properties></object>
      <object caption='Returns'><properties>
        <relation connection='excel-direct.0' name='Returns' table='[Returns$]' type='table' />
      </properties></object>
    </objects>
  </_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
</datasource>"""


# Faithful modern Azure SQL (`azure_sqldb`) Superstore .tds: a federated named-connection of
# class 'azure_sqldb', three independent physical tables wrapped in a <relation type='collection'>
# and duplicated under the object-model layer, with typed columns in <metadata-records>. Mirrors
# the live validation datasource (Orders / People / Returns on Azure SQL) so the exact deploy-ready
# M is pinned offline. Server/credentials here are placeholders -- never real values.
AZURE_SQL_SUPERSTORE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Superstore (Azure SQL)' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='azuresql' name='azure_sqldb.0a1b2c'>
        <connection authentication='sqlserver' class='azure_sqldb' dbname='Superstore'
                    server='example.database.windows.net' username='svc' />
      </named-connection>
    </named-connections>
    <relation type='collection'>
      <relation connection='azure_sqldb.0a1b2c' name='Orders' table='[dbo].[Orders]' type='table' />
      <relation connection='azure_sqldb.0a1b2c' name='People' table='[dbo].[People]' type='table' />
      <relation connection='azure_sqldb.0a1b2c' name='Returns' table='[dbo].[Returns]' type='table' />
    </relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Order ID</remote-name><local-name>[Order ID]</local-name>
        <parent-name>[Orders]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Person</remote-name><local-name>[Person]</local-name>
        <parent-name>[People]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[People]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Returned</remote-name><local-name>[Returned]</local-name>
        <parent-name>[Returns]</parent-name><local-type>boolean</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
    <objects>
      <object caption='Orders'><properties>
        <relation connection='azure_sqldb.0a1b2c' name='Orders' table='[dbo].[Orders]' type='table' />
      </properties></object>
      <object caption='People'><properties>
        <relation connection='azure_sqldb.0a1b2c' name='People' table='[dbo].[People]' type='table' />
      </properties></object>
      <object caption='Returns'><properties>
        <relation connection='azure_sqldb.0a1b2c' name='Returns' table='[dbo].[Returns]' type='table' />
      </properties></object>
    </objects>
  </_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
</datasource>"""


# -- type mapping --------------------------------------------------------------
@pytest.mark.parametrize("local,expected", [
    ("integer", "int64"), ("real", "double"), ("string", "string"),
    ("boolean", "boolean"), ("date", "dateTime"), ("datetime", "dateTime"),
    ("table", None), ("spatial", None), ("", None),
])
def test_tableau_type_mapping(local, expected):
    assert tableau_type_to_tmdl(local) == expected


# -- parsing -------------------------------------------------------------------
def test_parse_live_sqlserver():
    d = parse_tds(LIVE_SQLSERVER)
    assert d["connection_class"] == "sqlserver"
    assert d["server"] == "myserver.database.windows.net"
    assert d["database"] == "Superstore"
    assert d["is_extract"] is False
    assert d["named_connection_count"] == 1
    assert len(d["relations"]) == 1
    rel = d["relations"][0]
    assert rel["kind"] == "table"
    assert rel["schema"] == "dbo"
    assert rel["item"] == "Orders"
    assert {c["remote_name"] for c in rel["columns"]} == {"Order ID", "Sales", "Quantity"}
    assert {c["tmdl_type"] for c in rel["columns"]} == {"string", "double", "int64"}


def test_parse_never_carries_credentials():
    d = parse_tds(LIVE_SQLSERVER)
    blob = repr(d)
    assert "username" not in blob and "svc" not in blob


def test_parse_extract_flag_and_does_not_inflate_connection_count():
    d = parse_tds(EXTRACT_OVER_SQLSERVER)
    assert d["is_extract"] is True
    # the hyper connection inside <extract> must NOT be counted as a second named connection.
    assert d["named_connection_count"] == 1
    assert d["connection_class"] == "sqlserver"


def test_parse_extract_object_model_drops_cache_twins():
    # The modern Server-materialized extract carries each live relation PLUS a `[Extract].[...]_HASH`
    # cache twin. parse_tds must keep only the live/logical relations (the twins would bind to a
    # non-existent Delta entity in a DirectLake rebuild).
    d = parse_tds(EXTRACT_OBJECT_MODEL)
    assert d["is_extract"] is True
    assert d["connection_class"] == "databricks"
    tables = [r for r in d["relations"] if r["kind"] == "table"]
    assert [r["name"] for r in tables] == ["sales", "people"]
    # no `[Extract]` cache twin survives, in either the collection layer or the object-graph.
    assert not any("Extract" in (r.get("raw_table") or "") for r in tables)
    by_name = {r["name"]: r for r in tables}
    # the surviving live relations keep their resolved columns + their live upstream coordinates.
    assert {c["remote_name"] for c in by_name["sales"]["columns"]} == {"Amount"}
    assert by_name["sales"]["catalog"] == "salesdb" and by_name["sales"]["schema"] == "core"
    assert d["unsupported_reasons"] == []


def test_parse_extract_only_keeps_extract_tables():
    # With NO live relation to replace them, the `[Extract]` tables are all the source has -> keep.
    d = parse_tds(EXTRACT_ONLY)
    tables = [r for r in d["relations"] if r["kind"] == "table"]
    assert [r["name"] for r in tables] == ["sales_AB12"]
    assert {c["remote_name"] for c in tables[0]["columns"]} == {"Amount"}


def test_bare_hyper_connection_class_detected_as_extract():
    # A standalone .hyper datasource (connection class 'hyper', no <extract enabled> wrapper) IS a
    # materialized extract -> is_extract must fire on the class alone so it routes to offline Import
    # instead of dying at needs-decision. (EXTRACT_ONLY has no <extract> element.)
    assert parse_tds(EXTRACT_ONLY)["is_extract"] is True


def test_federated_hyper_named_connection_detected_as_extract():
    # The extract-engine class is detected through a federated wrapper's named-connection too.
    fed_hyper = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Sales' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections><named-connection caption='ex' name='hyper.1'>
      <connection class='hyper' dbname='sales.hyper' /></named-connection></named-connections>
    <relation connection='hyper.1' name='Orders' table='[Extract].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'><remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type></metadata-record>
    </metadata-records>
  </connection>
</datasource>"""
    d = parse_tds(fed_hyper)
    assert d["connection_class"] == "hyper"
    assert d["is_extract"] is True


def test_live_connection_not_falsely_flagged_as_extract():
    # Guard: a genuine live source with no extract is NOT flagged (the class check must be narrow).
    assert parse_tds(LIVE_SQLSERVER)["is_extract"] is False



def test_parse_custom_sql_relation():
    d = parse_tds(CUSTOM_SQL)
    rel = d["relations"][0]
    assert rel["kind"] == "custom_sql"
    assert "GROUP BY" in rel["sql"]
    assert len(rel["columns"]) == 2


def test_parse_join_tree_surfaces_leaf_tables():
    d = parse_tds(JOIN_TREE)
    # A physical join tree is NOT collapsed: its leaf tables surface as independent model tables
    # (the container `join` entry is dropped) so the source rebuilds as a multi-table model, exactly
    # like a multi-table object-graph source.
    kinds = [r["kind"] for r in d["relations"]]
    assert kinds == ["table", "table"]
    assert sorted(r["name"] for r in d["relations"]) == ["Orders", "People"]
    assert "join" not in kinds
    # This degenerate fixture's join clause carries no operands, so no relationship is inferred.
    assert d["relationships"] == []


def test_parse_physical_join_surfaces_leaves_with_columns():
    d = parse_tds(PHYSICAL_JOIN_KEYS)
    tables = [r for r in d["relations"] if r["kind"] == "table"]
    # every leaf table surfaces (no `join` container kind leaks out) WITH its resolved columns.
    assert sorted(r["name"] for r in tables) == ["Customer", "Manager", "Orders"]
    assert "join" not in {r["kind"] for r in d["relations"]}
    by_name = {r["name"]: r for r in tables}
    assert {c["remote_name"] for c in by_name["Orders"]["columns"]} == {
        "Id", "CustomerId", "ManagerId"}
    # the role-playing alias `Manager` (over the physical [Customer] table) stays a DISTINCT table
    # carrying the physical table's columns -- it does not collapse into the base `Customer` table.
    assert {c["remote_name"] for c in by_name["Manager"]["columns"]} == {"Id", "Name"}
    assert {c["remote_name"] for c in by_name["Customer"]["columns"]} == {"Id", "Name"}


def test_parse_physical_join_recovers_relationships_from_clauses():
    d = parse_tds(PHYSICAL_JOIN_KEYS)
    rels = {(r["from_table"], r["from_col"], r["to_table"], r["to_col"])
            for r in d["relationships"]}
    # each physical join <clause> equality predicate becomes a model relationship, keyed to the
    # surfaced leaf tables -- including the one onto the role-playing alias `Manager`.
    assert rels == {
        ("Orders", "CustomerId", "Customer", "Id"),
        ("Orders", "ManagerId", "Manager", "Id"),
    }
    # a physical join is uniqueness-agnostic -> emitted many_to_many (crash-proof), like the noodle.
    assert all(r["cardinality"] == "many_to_many" for r in d["relationships"])


def test_parse_excel_collection_yields_independent_deduped_tables():
    d = parse_tds(EXCEL_COLLECTION)
    assert d["connection_class"] == "excel-direct"
    assert d["is_extract"] is False
    # collection container is dropped; the 3 sheets become independent tables (no duplicates
    # from the <properties> object-model layer), and none are mis-flagged as a join/union.
    assert [r["kind"] for r in d["relations"]] == ["table", "table", "table"]
    names = {r["name"] for r in d["relations"]}
    assert names == {"Orders", "People", "Returns"}
    by_name = {r["name"]: r for r in d["relations"]}
    assert {c["remote_name"] for c in by_name["Orders"]["columns"]} == {"Row ID", "Sales"}
    assert {c["remote_name"] for c in by_name["People"]["columns"]} == {"Person", "Region"}
    assert d["unsupported_reasons"] == []


def test_excel_collection_selects_import_not_fallback():
    from storage_mode import select_storage_mode
    decision = select_storage_mode(parse_tds(EXCEL_COLLECTION))
    # a container of independent sheets is a clean multi-table Import, never a join fallback.
    assert decision["mode"] == "Import"
    assert decision["connector"] == "Excel.Workbook"
    assert decision["fallback"] is None


# -- M emission ----------------------------------------------------------------
def test_emit_connection_parameters():
    d = parse_tds(LIVE_SQLSERVER)
    params = emit_connection_parameters(d)
    assert 'expression Server = "myserver.database.windows.net"' in params
    assert 'expression Database = "Superstore"' in params
    assert "IsParameterQuery=true" in params


def test_emit_directquery_table_partition():
    d = parse_tds(LIVE_SQLSERVER)
    tmdl = emit_table_tmdl_m(d["relations"][0], d, "DirectQuery")
    assert "partition Orders = m" in tmdl
    assert "mode: directQuery" in tmdl
    assert 'Source = Sql.Database(#"Server", #"Database")' in tmdl
    assert 'Source{[Schema="dbo", Item="Orders"]}[Data]' in tmdl
    # columns are typed from Tableau metadata, not deferred to PBI inference.
    assert "dataType: int64" in tmdl     # Quantity
    assert "dataType: double" in tmdl    # Sales
    assert "sourceColumn: Sales" in tmdl


def test_emit_import_mode_keyword():
    d = parse_tds(LIVE_SQLSERVER)
    tmdl = emit_table_tmdl_m(d["relations"][0], d, "Import")
    assert "mode: import" in tmdl


def test_emit_custom_sql_uses_native_query_with_folding():
    d = parse_tds(CUSTOM_SQL)
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert "Value.NativeQuery(Source" in body
    assert "[EnableFolding=true]" in body
    # embedded double quotes in the SQL are escaped for the M string literal.
    assert '""Region""' in body


def test_escape_m_string_escapes_control_chars_for_single_line_literal():
    # A complete M double-quoted-literal escaper: quotes doubled, and CR/LF/tab become M character
    # escapes so a multi-line value stays on ONE physical line (TMDL indentation safety).
    assert escape_m_string('a"b') == 'a""b'
    assert escape_m_string("a\r\nb") == "a#(lf)b"      # CRLF normalized to a single #(lf)
    assert escape_m_string("a\nb") == "a#(lf)b"
    assert escape_m_string("a\rb") == "a#(lf)b"
    assert escape_m_string("a\tb") == "a#(tab)b"
    # identifier-shaped values carry no control chars -> byte-identical output (other callers safe).
    assert escape_m_string("myserver.database.windows.net") == "myserver.database.windows.net"
    assert escape_m_string("") == ""
    assert escape_m_string(None) == ""


def test_emit_multiline_custom_sql_native_query_stays_single_line():
    # Regression (Defect A): multi-line Custom SQL must not leak raw newlines into the
    # Value.NativeQuery M literal -- interior SQL lines at column 0 break the indentation-significant
    # TMDL partition (Fabric: Workload_FailedToParseFile -- Invalid indentation).
    d = parse_tds(CUSTOM_SQL)
    rel = dict(d["relations"][0])
    rel["sql"] = "SELECT o.id, o.amount\r\nFROM orders o\r\nJOIN detail d ON d.id = o.id"
    body = emit_m_partition_source(rel, d, "DirectQuery")
    assert "Value.NativeQuery" in body
    # the SQL is folded onto one line with escaped newlines...
    assert "o.amount#(lf)FROM orders o#(lf)JOIN detail d ON d.id = o.id" in body
    # ...so NO interior SQL line lands at column 0, and a source CRLF does not double into a blank line.
    assert "\nFROM orders o" not in body
    assert "\nJOIN detail" not in body
    assert "#(lf)#(lf)" not in body


def test_emit_multiline_odbc_custom_sql_stays_single_line():
    # Same indentation-safety guarantee for the generic-ODBC Odbc.Query custom-SQL path (Defect A).
    d = parse_tds(GENERIC_ODBC_DRIVER)
    rel = dict(d["relations"][0])
    rel["sql"] = "SELECT a,\r\nb\r\nFROM lake.orders"
    body = emit_m_partition_source(rel, d, "Import")
    assert "Odbc.Query" in body
    assert "SELECT a,#(lf)b#(lf)FROM lake.orders" in body
    assert "\nFROM lake.orders" not in body
    assert "#(lf)#(lf)" not in body


def test_emit_oracle_table_is_deploy_ready_server_only_m():
    # Oracle.Database is server-only (service/SID embedded in the server); flat schema/item
    # navigation with hierarchy off. No unused #"Database" parameter is carried.
    d = parse_tds(ORACLE)
    assert d["connection_class"] == "oracle"
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert 'Source = Oracle.Database(#"Server", [HierarchicalNavigation=false])' in body
    assert 'Source{[Schema="SALES", Item="ORDERS"]}[Data]' in body
    assert "TODO" not in body
    assert '#"Database"' not in body            # Oracle's database is in the server string
    params = emit_connection_parameters(d)
    assert 'expression Server = "oradb.example.com:1521/ORCL"' in params
    assert "Database" not in params             # no unused database parameter


# -- generic ODBC (engine-agnostic lift-and-shift) -----------------------------
def test_odbc_connection_string_driver_form_scrubs_secrets():
    conn = {"odbc_driver": "Simba Trino ODBC Driver", "odbc_dsn": "",
            "server": "trino.local", "port": "8080", "database": "lake",
            "odbc_connect_string_extras": "SSL=true;UID=admin;PWD=secret;Region=us-east-1"}
    cs = _odbc_connection_string(conn)
    assert cs == ("Driver={Simba Trino ODBC Driver};Server=trino.local;Port=8080;"
                  "Database=lake;SSL=true;Region=us-east-1")
    # credential-bearing extras are dropped.
    assert "UID" not in cs and "admin" not in cs
    assert "PWD" not in cs and "secret" not in cs


def test_odbc_connection_string_dsn_form_wins_over_driver():
    # when both are present the DSN wins (it encapsulates the driver + host).
    conn = {"odbc_dsn": "Bamboo DSN", "odbc_driver": "PostgreSQL Unicode(x64)",
            "server": "x", "port": "1", "database": "d"}
    assert _odbc_connection_string(conn) == "dsn=Bamboo DSN"


def test_odbc_connection_string_none_when_neither_dsn_nor_driver():
    assert _odbc_connection_string({"odbc_dsn": "", "odbc_driver": ""}) is None


def test_scrub_odbc_extras_drops_credentials_keeps_rest():
    out = _scrub_odbc_extras("UID=admin;PWD=secret;Token=abc;SSL=true;Region=us")
    assert out == "SSL=true;Region=us"
    assert _scrub_odbc_extras("") == ""
    assert _scrub_odbc_extras(None) == ""


def test_parse_genericodbc_descriptor_carries_odbc_facts():
    d = parse_tds(GENERIC_ODBC_DRIVER)
    assert d["connection_class"] == "genericodbc"
    assert d["odbc_driver"] == "Simba Trino ODBC Driver"
    assert d["odbc_dsn"] == ""
    assert d["server"] == "trino.minio.local"
    assert d["port"] == "8080"
    assert d["database"] == "lake"
    # the descriptor is serialized into the report, so the extras are scrubbed of inline creds.
    assert d["odbc_connect_string_extras"] == "SSL=true"
    assert "hunter2" not in (d["odbc_connect_string_extras"] or "")
    assert d["odbc_dbms_name"] == "Trino"
    rel = d["relations"][0]
    assert rel["kind"] == "custom_sql"


def test_emit_genericodbc_custom_sql_uses_odbc_query():
    d = parse_tds(GENERIC_ODBC_DRIVER)
    body = emit_m_partition_source(d["relations"][0], d, "Import")
    # the driver-form connection string is rebuilt and the custom SQL passes straight through.
    assert 'Source = Odbc.Query("Driver={Simba Trino ODBC Driver};' in body
    assert "Server=trino.minio.local;Port=8080;Database=lake;SSL=true" in body
    assert "FROM lake.orders" in body
    # embedded double quotes in the SQL are escaped for the M string literal.
    assert '""region""' in body
    # not a scaffold.
    assert "TODO" not in body
    assert m_partition_review_reason(d["relations"][0], d, "Import") is None


def test_emit_genericodbc_never_emits_inline_secret():
    # the .tds carries inline username/password AND UID/PWD in the extras; NONE may reach the M.
    d = parse_tds(GENERIC_ODBC_DRIVER)
    body = emit_m_partition_source(d["relations"][0], d, "Import")
    assert "hunter2" not in body
    assert "PWD" not in body
    assert "UID" not in body
    # the non-secret extra is preserved.
    assert "SSL=true" in body


def test_genericodbc_emits_no_connection_parameters():
    # Odbc.Query inlines the whole connection string, so no #"Server"/#"Database" params are emitted.
    d = parse_tds(GENERIC_ODBC_DRIVER)
    assert emit_connection_parameters(d) == ""


def test_emit_genericodbc_dsn_table_relation_scaffolds_odbc_datasource():
    d = parse_tds(GENERIC_ODBC_DSN)
    assert d["odbc_dsn"] == "Bamboo DSN"
    rel = d["relations"][0]
    assert rel["kind"] == "table"
    body = emit_m_partition_source(rel, d, "Import")
    # generic-ODBC table navigation isn't portable -> a clearly-flagged Odbc.DataSource scaffold.
    assert "TODO" in body
    assert "Odbc.DataSource" in body
    reason = m_partition_review_reason(rel, d, "Import")
    assert reason is not None and "driver-specific" in reason


def test_genericodbc_storage_routes_to_import_odbc_query():
    from storage_mode import select_storage_mode
    decision = select_storage_mode(parse_tds(GENERIC_ODBC_DRIVER))
    assert decision["mode"] == "Import"
    assert decision["connector"] == "Odbc.Query"
    assert decision["uses_native_query"] is True
    assert decision["fallback"] is None


# -- native query engines over ODBC (Spark / Presto / Trino / Starburst) --------------------------
# A native engine .tds carries server/port/catalog but NO odbc-driver attribute (Tableau used its
# bundled native driver). The emitter must synthesize the per-engine driver and bind over ODBC.
def _native_engine_tds(cls, custom_sql=True):
    if custom_sql:
        rel = (f"<relation connection='{cls}.abc' name='Custom SQL Query' type='text'>"
               'SELECT "region", SUM(sales) AS total FROM hive.orders GROUP BY "region"</relation>')
        parent = "[Custom SQL Query]"
    else:
        rel = f"<relation connection='{cls}.abc' name='orders' table='[orders]' type='table' />"
        parent = "[orders]"
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Lakehouse' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='engine' name='{cls}.abc'>
        <connection class='{cls}' dbname='hive' port='8080' server='engine.example.com'
                    username='analyst' password='hunter2' />
      </named-connection>
    </named-connections>
    {rel}
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>region</remote-name><local-name>[region]</local-name>
        <parent-name>{parent}</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>total</remote-name><local-name>[total]</local-name>
        <parent-name>{parent}</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""


_NATIVE_ENGINE_DRIVER = {
    "spark": "Simba Spark ODBC Driver",
    "presto": "Simba Presto ODBC Driver",
    "trino": "Starburst ODBC Driver for Trino",
    "starburst": "Starburst ODBC Driver for Trino",
}


@pytest.mark.parametrize("cls", ["spark", "presto", "trino", "starburst"])
def test_parse_native_engine_descriptor_has_facts_but_no_odbc_driver(cls):
    d = parse_tds(_native_engine_tds(cls))
    assert d["connection_class"] == cls
    assert d["server"] == "engine.example.com"
    assert d["port"] == "8080"
    assert d["database"] == "hive"
    # the native .tds carries no ODBC driver/DSN -- the emitter supplies one.
    assert not (d.get("odbc_driver") or "")
    assert not (d.get("odbc_dsn") or "")
    assert d["relations"][0]["kind"] == "custom_sql"


@pytest.mark.parametrize("cls", ["spark", "presto", "trino", "starburst"])
def test_emit_native_engine_custom_sql_uses_odbc_query_with_synth_driver(cls):
    d = parse_tds(_native_engine_tds(cls))
    body = emit_m_partition_source(d["relations"][0], d, "Import")
    driver = _NATIVE_ENGINE_DRIVER[cls]
    assert f'Source = Odbc.Query("Driver={{{driver}}};' in body
    assert "Server=engine.example.com;Port=8080;Database=hive" in body
    assert "FROM hive.orders" in body
    # a clean, deploy-ready partition (not a scaffold).
    assert "TODO" not in body
    assert m_partition_review_reason(d["relations"][0], d, "Import") is None


@pytest.mark.parametrize("cls", ["spark", "presto", "trino", "starburst"])
def test_emit_native_engine_never_emits_inline_secret(cls):
    # the inner <connection> carries username/password; NEITHER may reach the M body.
    d = parse_tds(_native_engine_tds(cls))
    body = emit_m_partition_source(d["relations"][0], d, "Import")
    assert "hunter2" not in body
    assert "analyst" not in body
    assert "password" not in body.lower()


@pytest.mark.parametrize("cls", ["spark", "presto", "trino", "starburst"])
def test_native_engine_emits_no_connection_parameters(cls):
    # Odbc.Query inlines the whole connection string -> no #"Server"/#"Database" params.
    d = parse_tds(_native_engine_tds(cls))
    assert emit_connection_parameters(d) == ""


@pytest.mark.parametrize("cls", ["spark", "presto", "trino", "starburst"])
def test_emit_native_engine_table_relation_scaffolds_odbc_datasource(cls):
    d = parse_tds(_native_engine_tds(cls, custom_sql=False))
    rel = d["relations"][0]
    assert rel["kind"] == "table"
    body = emit_m_partition_source(rel, d, "Import")
    # ODBC table navigation is driver-specific -> a clearly-flagged Odbc.DataSource scaffold,
    # but still NOT a land-to-Delta fallback.
    assert "TODO" in body
    assert "Odbc.DataSource" in body
    assert m_partition_review_reason(rel, d, "Import") is not None


def test_unmapped_connector_scaffold_reason_points_at_needs_decision_not_land_to_delta():
    # The de-default contract reaches the partition emitter too: when a wholly-unmapped connector
    # class scaffolds, its needs-review reason must point at the honest needs-storage-decision path
    # (rebuild direct-to-source; land-to-Delta + DirectLake framed as an explicit opt-in) and must
    # NEVER instruct the user that the router auto-routes to land-to-Delta + DirectLake.
    d = {"connection_class": "acmewarehouse", "named_connection_count": 1,
         "relations": [{"kind": "table", "name": "T", "item": "T",
                        "columns": [{"model_name": "x", "tmdl_type": "int64"}]}]}
    rel = d["relations"][0]
    reason = m_partition_review_reason(rel, d, "Import")
    assert reason is not None
    low = reason.lower()
    assert "not mapped" in low
    # the honest de-default: a storage decision is required, DirectLake only via opt-in
    assert "storage decision" in low
    assert ("opt in" in low or "opt-in" in low) and "never auto-selected" in low
    # the OLD auto-route wording is gone (the scaffold no longer directs to land-to-Delta)
    assert "route to land-to-delta + directlake" not in low


def test_emit_snowflake_table_is_deploy_ready_three_level_navigation():
    # Snowflake.Databases(server, warehouse) then database -> schema -> table, keyed by [Name, Kind].
    d = parse_tds(SNOWFLAKE)
    assert d["connection_class"] == "snowflake"
    assert d["warehouse"] == "COMPUTE_WH"
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert 'Source = Snowflake.Databases(#"Server", #"Warehouse")' in body
    assert 'Source{[Name="ANALYTICS", Kind="Database"]}[Data]' in body
    assert 'Db{[Name="PUBLIC", Kind="Schema"]}[Data]' in body
    assert 'Schema{[Name="ORDERS", Kind="Table"]}[Data]' in body
    assert "TODO" not in body
    assert "Sql.Database" not in body
    # the warehouse is parameterized (declared from the .tds), not hardcoded into the call.
    params = emit_connection_parameters(d)
    assert 'expression Warehouse = "COMPUTE_WH"' in params
    assert 'expression Server = "acct.snowflakecomputing.com"' in params
    assert "Database" not in params             # Snowflake reaches the database by navigation


def test_emit_snowflake_scaffolds_when_database_missing():
    # Without a resolvable database the first navigation hop can't be built -> scaffold, not a guess.
    d = parse_tds(SNOWFLAKE)
    d["database"] = None
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert "TODO" in body
    assert "Snowflake.Databases" in body
    assert "[Name=" not in body


# Each fully-supported connector takes the verified `(server, database)` signature, so the
# two-argument call + Schema/Item navigation is emitted as deploy-ready M.
@pytest.mark.parametrize("cls,connector", [
    ("sqlserver", "Sql.Database"),
    ("azure_sqldb", "Sql.Database"),
    ("postgres", "PostgreSQL.Database"),
    ("mysql", "MySQL.Database"),
    ("redshift", "AmazonRedshift.Database"),
])
def test_emit_fully_supported_connector_dispatch(cls, connector):
    rel = {"kind": "table", "name": "Orders", "item": "Orders", "schema": "dbo", "columns": []}
    body = emit_m_partition_source(rel, {"connection_class": cls}, "DirectQuery")
    assert f'Source = {connector}(#"Server", #"Database")' in body
    assert 'Source{[Schema="dbo", Item="Orders"]}[Data]' in body


# Recognized connectors we deliberately do NOT auto-emit yet: the body must be a named scaffold
# that hints the intended connector, never a guessed call (BigQuery has no M function reference
# page, so its navigation selectors / project identifiers aren't verifiable offline).
@pytest.mark.parametrize("cls,connector", [
    ("bigquery", "GoogleBigQuery.Database"),
])
def test_emit_partial_connector_is_named_scaffold_not_guessed_m(cls, connector):
    rel = {"kind": "table", "name": "T", "item": "T", "schema": "s", "columns": []}
    body = emit_m_partition_source(rel, {"connection_class": cls}, "DirectQuery")
    assert "TODO" in body
    assert connector in body                     # names the intended connector as a hint
    assert '(#"Server", #"Database")' not in body  # but never a guessed 2-arg upstream call
    assert "Sql.Database" not in body


def test_emit_unsupported_class_falls_back_to_scaffold():
    # A connector class outside the verified set is emitted as a bare scaffold, never wrong M.
    rel = {"kind": "table", "name": "T", "item": "T", "schema": "s", "columns": []}
    body = emit_m_partition_source(rel, {"connection_class": "saphana"}, "Import")
    assert "TODO" in body
    assert "'saphana'" in body
    assert '(#"Server", #"Database")' not in body


def test_emit_teradata_parsed_is_scaffold_pending_live_navigator():
    # Teradata.Database(server, [options]) has a documented server-only signature, but with no live
    # Teradata navigator to confirm the emitted body actually binds, it is held as a flagged
    # scaffold (recognized + named) rather than shipped as deploy-ready M we have never resolved.
    d = parse_tds(TERADATA)
    assert d["connection_class"] == "teradata"
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert "TODO" in body
    assert "'teradata'" in body
    assert "Teradata.Database" in body                 # names the intended connector as a hint
    assert 'Source = Teradata.Database(#"Server"' not in body   # but never a guessed call body
    # The scaffold must be DEPLOY-valid TMDL: a single `let ... in` expression (comment inside the
    # block), with an inert empty typed table as the source rather than the old `Source = null`.
    assert body.startswith("let\n")
    assert body.rstrip().endswith("Source")
    assert "Source = #table(type table [], {})" in body
    assert "Source = null" not in body
    # one expression: the body starts with `let` and the // TODO comment lives INSIDE the block
    # (after the `let` line), never as a bare sibling that the TMDL parser rejects.
    lines = body.split("\n")
    assert lines[0] == "let"
    assert lines[1].lstrip().startswith("// TODO")


def test_emit_fabric_sql_endpoint_is_deploy_ready_sql_database():
    # Microsoft Fabric Warehouse / Lakehouse SQL endpoint speaks the SQL Server TDS protocol ->
    # Sql.Database(server, database), identical to the sqlserver / azure_sqldb path.
    d = parse_tds(FABRIC_SQL)
    assert d["connection_class"] == "microsoft_fabric_sql_endpoint"
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert 'Source = Sql.Database(#"Server", #"Database")' in body
    assert 'Source{[Schema="dbo", Item="Orders"]}[Data]' in body
    assert "TODO" not in body


def test_emit_synapse_is_deploy_ready_sql_database():
    # Azure Synapse Analytics speaks the SQL Server TDS protocol -> Sql.Database, byte-identical
    # to the sqlserver / azure_sqldb path.
    d = parse_tds(SYNAPSE)
    assert d["connection_class"] == "azure_sql_dw"
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert 'Source = Sql.Database(#"Server", #"Database")' in body
    assert 'Source{[Schema="dbo", Item="Orders"]}[Data]' in body
    assert "TODO" not in body
    params = emit_connection_parameters(d)
    assert 'expression Server = "syn.sql.azuresynapse.net"' in params
    assert 'expression Database = "WideWorld"' in params


def test_emit_managed_instance_is_deploy_ready_sql_database():
    # Azure SQL Managed Instance reaches Tableau through the SQL Server connector (class
    # 'sqlserver'), so it must bind through Sql.Database like any other SQL Server endpoint -- the
    # instance-specific host (here carrying a port) round-trips verbatim into the #"Server" param.
    from storage_mode import select_storage_mode
    d = parse_tds(MANAGED_INSTANCE)
    assert d["connection_class"] == "sqlserver"
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert 'Source = Sql.Database(#"Server", #"Database")' in body
    assert 'Source{[Schema="dbo", Item="Orders"]}[Data]' in body
    assert "TODO" not in body
    params = emit_connection_parameters(d)
    assert 'expression Server = "myinst.public.0a1b2c3d4e5f.database.windows.net,3342"' in params
    assert 'expression Database = "Sales"' in params
    decision = select_storage_mode(d)
    assert decision["mode"] == "DirectQuery"
    assert decision["connector"] == "Sql.Database"
    assert decision["fully_supported"] is True
    assert decision["recommended_mode"] == "DirectQuery"
    assert decision["fallback"] is None


def test_emit_synapse_serverless_is_deploy_ready_sql_database():
    # The Synapse connector emits the same class ('azure_sql_dw') for the serverless (on-demand)
    # endpoint as for a dedicated pool, so a serverless workspace endpoint binds through
    # Sql.Database identically -- no separate class, no scaffold.
    from storage_mode import select_storage_mode
    d = parse_tds(SYNAPSE_SERVERLESS)
    assert d["connection_class"] == "azure_sql_dw"
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert 'Source = Sql.Database(#"Server", #"Database")' in body
    assert 'Source{[Schema="dbo", Item="Orders"]}[Data]' in body
    assert "TODO" not in body
    params = emit_connection_parameters(d)
    assert 'expression Server = "myws-ondemand.sql.azuresynapse.net"' in params
    assert 'expression Database = "Lake"' in params
    decision = select_storage_mode(d)
    assert decision["mode"] == "DirectQuery"
    assert decision["connector"] == "Sql.Database"
    assert decision["fully_supported"] is True
    assert decision["recommended_mode"] == "DirectQuery"
    assert decision["fallback"] is None


def test_emit_databricks_table_is_deploy_ready_catalogs_navigation():
    # Databricks.Catalogs(host, httpPath) then catalog -> schema -> table, keyed [Name, Kind]
    # (catalog level is Kind="Database"). Server + HttpPath are parameterized; no Database param.
    d = parse_tds(DATABRICKS)
    assert d["connection_class"] == "databricks"
    assert d["http_path"] == "/sql/1.0/warehouses/abc123"
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert 'Source = Databricks.Catalogs(#"Server", #"HttpPath")' in body
    assert 'Source{[Name="main", Kind="Database"]}[Data]' in body
    assert 'Db{[Name="sales", Kind="Schema"]}[Data]' in body
    assert 'Schema{[Name="orders", Kind="Table"]}[Data]' in body
    assert "TODO" not in body
    assert "Sql.Database" not in body
    params = emit_connection_parameters(d)
    assert 'expression Server = "adb-123.azuredatabricks.net"' in params
    assert 'expression HttpPath = "/sql/1.0/warehouses/abc123"' in params
    assert "Database" not in params            # the catalog is reached by navigation


def test_emit_databricks_scaffolds_when_catalog_missing():
    # Without a resolvable catalog (the first navigation hop) we scaffold rather than guess.
    d = parse_tds(DATABRICKS)
    d["database"] = None
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert "TODO" in body
    assert "Databricks.Catalogs" in body
    assert "[Name=" not in body


def test_emit_databricks_custom_sql_is_scaffold():
    # Even for an allow-listed connector, a custom-SQL relation with NO resolvable catalog (no
    # relation catalog and no connection database) can't be drilled, so it is a named scaffold --
    # never a guessed Value.NativeQuery against the Catalogs() root.
    rel = {"kind": "custom_sql", "name": "Q", "item": "Q", "sql": "SELECT 1", "columns": []}
    body = emit_m_partition_source(rel, {"connection_class": "databricks"}, "DirectQuery")
    assert "TODO" in body
    assert "Databricks.Catalogs" in body
    assert "Value.NativeQuery" not in body


def test_emit_databricks_custom_sql_drills_catalog_then_native_query():
    # Live-verified path: drill Databricks.Catalogs -> Kind="Database" handle, then fold the native
    # query against THAT drilled handle (never the Catalogs() root, which rejects native queries).
    d = parse_tds(DATABRICKS_CUSTOM_SQL)
    assert d["connection_class"] == "databricks"
    rel = d["relations"][0]
    assert rel["kind"] == "custom_sql"
    body = emit_m_partition_source(rel, d, "DirectQuery")
    assert "TODO" not in body
    assert 'Source = Databricks.Catalogs(#"Server", #"HttpPath")' in body
    # the catalog drill, then NativeQuery against the drilled Catalog handle
    assert 'Catalog = Source{[Name="tableau_migration_databricks", Kind="Database"]}[Data]' in body
    assert "Value.NativeQuery(Catalog, " in body
    assert "Value.NativeQuery(Source" not in body          # never against the root collection
    assert "[EnableFolding=true]" in body
    # NO rename in the M body: the native query returns the RAW source headers and each TMDL column
    # binds to that raw name via its sourceColumn (fold-safe). A Table.RenameColumns above a folded
    # native query breaks in Fabric ("The name 't0.Order_Date' doesn't exist in the current context").
    assert "Table.RenameColumns" not in body
    # build-time fail-loud: a real drilled partition is NOT flagged as needing review
    assert m_partition_review_reason(rel, d, "DirectQuery") is None


def test_emit_databricks_custom_sql_binds_raw_source_columns_in_tmdl():
    # The fold-safe binding for a spaced/special remote name lives in the TMDL as a quoted
    # sourceColumn (NOT a rename step in the M). The model column name stays underscored so DAX and
    # visual bindings are unaffected.
    d = parse_tds(DATABRICKS_CUSTOM_SQL)
    tmdl = emit_table_tmdl_m(d["relations"][0], d, "DirectQuery")
    assert "Table.RenameColumns" not in tmdl                       # never rename in M
    assert 'column Order_ID' in tmdl and 'sourceColumn: "Order ID"' in tmdl
    assert 'column Country_Region' in tmdl and 'sourceColumn: "Country/Region"' in tmdl
    assert "column Sales" in tmdl and "sourceColumn: Sales" in tmdl  # simple name stays bare


def test_emit_snowflake_custom_sql_still_scaffolds():
    # Snowflake shares the nav shape but is deliberately NOT in NATIVE_QUERY_CATALOG_DRILL, so its
    # custom SQL stays a flagged scaffold (and is reported needs_review), never auto-emitted.
    d = parse_tds(SNOWFLAKE_CUSTOM_SQL)
    assert d["connection_class"] == "snowflake"
    rel = d["relations"][0]
    assert rel["kind"] == "custom_sql"
    body = emit_m_partition_source(rel, d, "DirectQuery")
    assert "TODO" in body
    assert "Value.NativeQuery" not in body
    assert "Snowflake.Databases" in body                   # names the intended connector as a hint
    reason = m_partition_review_reason(rel, d, "DirectQuery")
    assert reason and "isn't verified" in reason


def test_emit_sqlserver_custom_sql_golden_m_unchanged():
    # Watch-item #1: the SQL Server family is the only EXISTING working custom-SQL path that the
    # refactor touches. Its columns (Region, Sales) need no rename, so NO rename step is emitted and
    # the body is byte-for-byte the historical form (the no-op guarantee).
    d = parse_tds(CUSTOM_SQL)
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    expected = (
        "let\n"
        '\t\t\t\tSource = Sql.Database(#"Server", #"Database"),\n'
        '\t\t\t\tResult = Value.NativeQuery(Source, "SELECT ""Region"", SUM(Sales) AS Sales '
        'FROM Orders GROUP BY ""Region""", null, [EnableFolding=true])\n'
        "\t\t\tin\n"
        "\t\t\t\tResult"
    )
    assert body == expected
    assert "Table.RenameColumns" not in body               # no mismatched names -> no rename step
    assert m_partition_review_reason(d["relations"][0], d, "DirectQuery") is None


# -- Custom SQL angle-bracket de-escape (Tableau doubles '<'/'>' on serialize) ---------
def test_deescape_custom_sql_operator_matrix():
    # The verified ground truth from controlled Databricks Superstore diagnostic saves: every
    # source operator is stored with its angle brackets doubled and must halve back exactly.
    assert _deescape_custom_sql("o.Profit << 0") == "o.Profit < 0"
    assert _deescape_custom_sql("o.Profit >> 1000") == "o.Profit > 1000"
    assert _deescape_custom_sql("o.Sales <<= 10") == "o.Sales <= 10"
    assert _deescape_custom_sql("o.Sales >>= 5000") == "o.Sales >= 5000"
    assert _deescape_custom_sql("o.Quantity <<>> 1") == "o.Quantity <> 1"


def test_deescape_custom_sql_hits_comments_and_string_literals():
    # The doubling is a blind global replace, so comments and string literals are affected too;
    # halving them back is correct (it restores the literal/comment the author actually wrote).
    assert _deescape_custom_sql("-- comment with << and >>") == "-- comment with < and >"
    assert _deescape_custom_sql("LIKE '%<<%'") == "LIKE '%<%'"


def test_deescape_custom_sql_reversible_for_genuine_multibracket():
    # A genuine source '<<tag>>' is stored '<<<<tag>>>>' and round-trips under a single halve;
    # a genuine Spark bitwise shift 'flags >> 2' is stored 'flags >>>> 2' and recovers exactly.
    assert _deescape_custom_sql("'<<<<tag>>>>'") == "'<<tag>>'"
    assert _deescape_custom_sql("flags >>>> 2") == "flags >> 2"


def test_deescape_custom_sql_is_not_idempotent():
    # Documents the single-call discipline: the parse boundary is the ONLY place this runs. A
    # second application would wrongly collapse a recovered genuine double.
    once = _deescape_custom_sql("flags >>>> 2")
    assert once == "flags >> 2"
    assert _deescape_custom_sql(once) == "flags > 2"        # the hazard a double-call would cause


def test_deescape_custom_sql_preserves_parameter_tokens():
    # The VERIFIED stored form is single-delimiter and bracketed: <[Parameters].[Name]>. Only the
    # literal operators around it are doubled. The token is masked before the halve and restored
    # verbatim, even when a doubled operator sits adjacent to it.
    assert (_deescape_custom_sql("WHERE o.Profit >> <[Parameters].[Threshold]>")
            == "WHERE o.Profit > <[Parameters].[Threshold]>")
    assert (_deescape_custom_sql("WHERE o.Sales << <[Parameters].[T]>")
            == "WHERE o.Sales < <[Parameters].[T]>")
    # The exact spelling captured from a live parameterized save round-trips and is detected.
    live = "WHERE o.Profit >> <[Parameters].[Parameter 0014036665946123]>   -- parameter"
    assert (_deescape_custom_sql(live)
            == "WHERE o.Profit > <[Parameters].[Parameter 0014036665946123]>   -- parameter")
    assert (custom_sql_parameter_refs(_deescape_custom_sql(live))
            == ["<[Parameters].[Parameter 0014036665946123]>"])
    assert (custom_sql_parameter_refs("a <[Parameters].[X]> and <[Parameters].[X]>")
            == ["<[Parameters].[X]>"])


def test_parse_tds_deescapes_custom_sql_at_the_boundary():
    # End-to-end: the descriptor's sql is already de-escaped, so NO downstream stage ever sees a
    # doubled operator. This is the actual reported bug (operators arrived doubled into the model).
    d = parse_tds(DATABRICKS_CUSTOM_SQL_DOUBLED)
    rel = d["relations"][0]
    assert rel["kind"] == "custom_sql"
    assert "<<" not in rel["sql"] and ">>" not in rel["sql"]
    assert "WHERE o.Profit < 0" in rel["sql"]
    assert "OR o.Sales > 1000" in rel["sql"]
    assert "OR o.Quantity <> 1" in rel["sql"]


def test_emit_databricks_doubled_custom_sql_emits_clean_native_query():
    # The emitted Databricks native query must carry the single-operator form and stay deploy-ready
    # (catalog drill + folding), with no review flag (no parameters here).
    d = parse_tds(DATABRICKS_CUSTOM_SQL_DOUBLED)
    rel = d["relations"][0]
    body = emit_m_partition_source(rel, d, "DirectQuery")
    assert "<<" not in body and ">>" not in body
    assert "Value.NativeQuery(Catalog, " in body
    assert "[EnableFolding=true]" in body
    assert m_partition_review_reason(rel, d, "DirectQuery") is None


def test_emit_databricks_custom_sql_flags_surviving_parameter():
    # A recovered <[Parameters].[Name]> token can't be translated yet, so the partition is still
    # emitted (deploy-valid) but flagged needs_review with the token named.
    d = parse_tds(DATABRICKS_CUSTOM_SQL_PARAM)
    rel = d["relations"][0]
    assert "<[Parameters].[Threshold]>" in rel["sql"]
    body = emit_m_partition_source(rel, d, "DirectQuery")
    assert "TODO" not in body                                # real query is emitted, not a scaffold
    assert "Value.NativeQuery(Catalog, " in body
    reason = m_partition_review_reason(rel, d, "DirectQuery")
    assert reason and "<[Parameters].[Threshold]>" in reason
    assert "parameter" in reason.lower()


def test_deescape_is_noop_on_already_correct_sql():
    # SQL with no doubled brackets (the common case for non-Databricks / equality-join custom SQL)
    # must pass through byte-for-byte: the existing SQL Server golden query is unchanged, and an
    # already-single-operator query is untouched.
    d = parse_tds(CUSTOM_SQL)
    assert _deescape_custom_sql(d["relations"][0]["sql"]) == d["relations"][0]["sql"]
    assert _deescape_custom_sql("SELECT * FROM t WHERE a = 1 AND b <> 2") == \
        "SELECT * FROM t WHERE a = 1 AND b <> 2"


# -- federated three-part-name collections (Tableau 2023+ object model) ---------
def test_parse_snowflake_federated_collection_yields_directquery_tables():
    # A <relation type='collection'> of three-part-name tables (duplicated under the logical layer)
    # must promote to independent kind='table' relations WITH columns + a parsed catalog/schema/item
    # -- and de-duplicate across the two layers -- so the datasource rebuilds as N DirectQuery tables
    # instead of collapsing to the land-to-Delta fallback.
    d = parse_tds(SNOWFLAKE_COLLECTION)
    assert d["connection_class"] == "snowflake"
    assert d["unsupported_reasons"] == []
    tables = [r for r in d["relations"] if r["kind"] == "table"]
    assert len(tables) == 2                                   # de-duplicated across both layers
    by_item = {t["item"]: t for t in tables}
    assert set(by_item) == {"INVOICE", "CUSTOMER"}
    assert by_item["INVOICE"]["catalog"] == "RETAILDB"
    assert by_item["INVOICE"]["schema"] == "SALESM"
    assert len(by_item["INVOICE"]["columns"]) == 2
    assert len(by_item["CUSTOMER"]["columns"]) == 2
    from storage_mode import select_storage_mode
    decision = select_storage_mode(d)
    assert decision["mode"] == "DirectQuery"
    assert decision["connector"] == "Snowflake.Databases"
    assert decision["fully_supported"] is True
    assert decision["fallback"] is None


def test_snowflake_collection_emits_three_part_navigation():
    d = parse_tds(SNOWFLAKE_COLLECTION)
    inv = next(r for r in d["relations"] if r.get("item") == "INVOICE")
    body = emit_m_partition_source(inv, d, "DirectQuery")
    assert 'Source = Snowflake.Databases(#"Server", #"Warehouse")' in body
    assert 'Source{[Name="RETAILDB", Kind="Database"]}[Data]' in body
    assert 'Db{[Name="SALESM", Kind="Schema"]}[Data]' in body
    assert 'Schema{[Name="INVOICE", Kind="Table"]}[Data]' in body
    assert "TODO" not in body


def test_snowflake_empty_warehouse_emits_flagged_param():
    # warehouse='' in the .tds -> keep #"Warehouse" so Snowflake.Databases stays a valid call, but
    # flag it loudly rather than silently emitting a broken empty-arg connection.
    d = parse_tds(SNOWFLAKE_COLLECTION)
    assert d["warehouse"] == ""
    params = emit_connection_parameters(d)
    assert 'expression Warehouse = ""' in params
    assert "TODO" in params and "warehouse" in params.lower()
    from storage_mode import select_storage_mode
    followups = select_storage_mode(d)["manual_followups"]
    assert any("Warehouse" in f for f in followups)


def test_snowflake_collection_auth_method_maps_to_basic_no_secret_leak():
    d = parse_tds(SNOWFLAKE_COLLECTION)
    assert d["auth_method"] == "Username Password"
    details = connection_details_for_bind(d)
    assert details["credential_kind"] == "Basic"               # 'Username Password' -> Basic
    blob = repr(d) + repr(details)
    assert "svc_loader" not in blob                            # the username value is never read


# -- extract_bundled_flatfile: lift a packaged Excel/CSV to an absolute path -----------------------
# A workbook/datasource extract backed by a flat file stores only a RELATIVE path in <connection>;
# Power BI's File.Contents rejects a relative path, so the Import model opens but loads no data. The
# fix lifts the bundled member out of the .tdsx/.twbx zip to an absolute on-disk path. Live database
# connections carry no bundled file (no flatfile_filename) and MUST be left untouched -> None.
import io as _io
import zipfile as _zip

_XLSX_BYTES = b"PK\x03\x04 fake-excel-bytes for the bundled flat file " + b"x" * 64


def _make_tdsx(members):
    """A minimal .tdsx/.twbx: an in-memory zip mapping member-path -> bytes."""
    buf = _io.BytesIO()
    with _zip.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_extract_bundled_flatfile_lifts_member_to_absolute_path(tmp_path):
    raw = _make_tdsx({
        "Superstore.tds": "<datasource/>",
        "Data/Datasources/Sample - Superstore.xlsx": _XLSX_BYTES,
    })
    src = tmp_path / "Superstore_-_Extract.tdsx"
    src.write_bytes(raw)
    dest = tmp_path / "data" / "Superstore_-_Extract"
    descriptor = {"flatfile_filename": "Data/Datasources/Sample - Superstore.xlsx"}

    out = extract_bundled_flatfile(str(src), descriptor, str(dest))

    import os
    assert out is not None
    assert os.path.isabs(out)                       # absolute -> File.Contents accepts it
    assert os.path.isfile(out)
    assert os.path.basename(out) == "Sample - Superstore.xlsx"
    with open(out, "rb") as fh:
        assert fh.read() == _XLSX_BYTES             # the real bytes were copied, not a stub


def test_extract_bundled_flatfile_unique_basename_fallback(tmp_path):
    # descriptor path does not match the member path exactly, but the basename is unique -> extracted
    raw = _make_tdsx({
        "Data/the-real-folder/Sample - Superstore.xlsx": _XLSX_BYTES,
    })
    descriptor = {"flatfile_filename": "Data/Datasources/Sample - Superstore.xlsx"}
    out = extract_bundled_flatfile(raw, descriptor, str(tmp_path / "out"))
    assert out is not None and out.lower().endswith("sample - superstore.xlsx")


def test_extract_bundled_flatfile_accepts_raw_zip_bytes(tmp_path):
    raw = _make_tdsx({"Data/Datasources/Sample - Superstore.xlsx": _XLSX_BYTES})
    descriptor = {"flatfile_filename": "Data/Datasources/Sample - Superstore.xlsx"}
    out = extract_bundled_flatfile(raw, descriptor, str(tmp_path / "out"))
    assert out is not None


def test_extract_bundled_flatfile_none_for_live_db_descriptor(tmp_path):
    # a Snowflake/Databricks/SQL Server connection has no bundled flat file -> NEVER touched
    raw = _make_tdsx({"Data/Datasources/Sample - Superstore.xlsx": _XLSX_BYTES})
    assert extract_bundled_flatfile(raw, {}, str(tmp_path / "out")) is None
    assert extract_bundled_flatfile(raw, {"flatfile_filename": None}, str(tmp_path / "out")) is None


def test_extract_bundled_flatfile_none_for_non_zip(tmp_path):
    # bare .tds/.twb XML text or a path to one (not a zip) -> keep the relative path unchanged
    descriptor = {"flatfile_filename": "Data/Datasources/Sample - Superstore.xlsx"}
    assert extract_bundled_flatfile("<datasource/>", descriptor, str(tmp_path / "out")) is None
    tds = tmp_path / "plain.tds"
    tds.write_text("<datasource/>", encoding="utf-8")
    assert extract_bundled_flatfile(str(tds), descriptor, str(tmp_path / "out")) is None


def test_extract_bundled_flatfile_none_for_missing_member(tmp_path):
    raw = _make_tdsx({"Data/Datasources/Other.xlsx": _XLSX_BYTES})
    descriptor = {"flatfile_filename": "Data/Datasources/Sample - Superstore.xlsx"}
    assert extract_bundled_flatfile(raw, descriptor, str(tmp_path / "out")) is None


def test_extract_bundled_flatfile_none_for_ambiguous_basename(tmp_path):
    # two members share the basename and neither matches the full relative path -> never guess
    raw = _make_tdsx({
        "Data/a/Sample - Superstore.xlsx": _XLSX_BYTES,
        "Data/b/Sample - Superstore.xlsx": _XLSX_BYTES,
    })
    descriptor = {"flatfile_filename": "Data/Datasources/Sample - Superstore.xlsx"}
    assert extract_bundled_flatfile(raw, descriptor, str(tmp_path / "out")) is None


def test_parse_databricks_federated_collection_yields_directquery_tables():
    d = parse_tds(DATABRICKS_COLLECTION)
    assert d["connection_class"] == "databricks"
    assert d["unsupported_reasons"] == []
    tables = [r for r in d["relations"] if r["kind"] == "table"]
    assert len(tables) == 2
    by_item = {t["item"]: t for t in tables}
    assert set(by_item) == {"WEB_EVENT", "ACCOUNT"}
    assert by_item["WEB_EVENT"]["catalog"] == "lakehouse_cat"
    assert by_item["WEB_EVENT"]["schema"] == "silver"
    assert len(by_item["WEB_EVENT"]["columns"]) == 2
    from storage_mode import select_storage_mode
    decision = select_storage_mode(d)
    assert decision["mode"] == "DirectQuery"
    assert decision["connector"] == "Databricks.Catalogs"
    assert decision["fully_supported"] is True
    assert decision["fallback"] is None


def test_databricks_collection_reads_v_http_path_and_emits_unity_nav():
    d = parse_tds(DATABRICKS_COLLECTION)
    assert d["http_path"] == "/sql/1.0/warehouses/cafe1234"    # read from the v-http-path attribute
    evt = next(r for r in d["relations"] if r.get("item") == "WEB_EVENT")
    body = emit_m_partition_source(evt, d, "DirectQuery")
    assert 'Source = Databricks.Catalogs(#"Server", #"HttpPath")' in body
    assert 'Source{[Name="lakehouse_cat", Kind="Database"]}[Data]' in body
    assert 'Db{[Name="silver", Kind="Schema"]}[Data]' in body
    assert 'Schema{[Name="WEB_EVENT", Kind="Table"]}[Data]' in body
    assert "TODO" not in body
    params = emit_connection_parameters(d)
    assert 'expression HttpPath = "/sql/1.0/warehouses/cafe1234"' in params


def test_databricks_collection_auth_method_maps_to_oauth_no_secret_leak():
    d = parse_tds(DATABRICKS_COLLECTION)
    assert d["auth_method"] == "oauth"
    details = connection_details_for_bind(d)
    assert details["credential_kind"] == "OAuth2"              # 'oauth' -> OAuth2
    blob = repr(d) + repr(details)
    for secret in ("svc_admin@example.com", "oauth-secret-id", "adb-evt.example.net/oidc"):
        assert secret not in blob                              # only non-secret fields are read


# -- object-graph relationships (P2) -------------------------------------------
def test_object_graph_relationships_parsed_with_model_names():
    # The two physical joins declared under <object-graph><relationships> become an explicit,
    # direction-preserving relationships list whose columns are the EMITTED model identifiers --
    # so "Order Key" (which clean_col turns into "Order_Key") is referenced as "Order_Key", not
    # the raw Tableau spelling, and a renamed endpoint "[REGION (REP)]" resolves to plain "REGION".
    d = parse_tds(FEDERATED_STAR)
    pairs = {(r["from_table"], r["from_col"], r["to_table"], r["to_col"])
             for r in d["relationships"]}
    assert pairs == {
        ("SALE", "REGION", "REP", "REGION"),
        ("SALE", "Order_Key", "RMA", "Order_Key"),
    }
    assert d["relationship_warnings"] == []
    # the join key with a space must surface as the cleaned identifier the table actually emits
    sale_cols = {c["model_name"] for c in
                 next(r for r in d["relations"] if r["name"] == "SALE")["columns"]}
    assert "Order_Key" in sale_cols and "Order Key" not in sale_cols


def test_object_graph_wrapped_tag_is_tolerated():
    # Tableau Desktop's logical model can wrap the object graph in a feature-flagged tag
    # (<_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>) instead of a plain <object-graph>.
    # The parser must match it by local-name suffix so relationship extraction still works on real
    # federated files -- otherwise the joins silently vanish (relationships == []).
    wrapped = FEDERATED_STAR.replace(
        "<object-graph>",
        "<_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>").replace(
        "</object-graph>",
        "</_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>")
    d = parse_tds(wrapped)
    pairs = {(r["from_table"], r["from_col"], r["to_table"], r["to_col"])
             for r in d["relationships"]}
    assert pairs == {
        ("SALE", "REGION", "REP", "REGION"),
        ("SALE", "Order_Key", "RMA", "Order_Key"),
    }
    assert d["relationship_warnings"] == []


def test_object_graph_relationships_do_not_flip_source_to_fallback():
    # A fuzzy/unused relationship must never demote an otherwise-supported datasource: relationship
    # warnings are tracked separately from unsupported_reasons, so the star still rebuilds 1:1.
    from storage_mode import select_storage_mode
    d = parse_tds(FEDERATED_STAR)
    decision = select_storage_mode(d)
    assert decision["fully_supported"] is True
    assert decision["mode"] == "DirectQuery"
    assert "relationship" not in " ".join(decision.get("unsupported_reasons", [])).lower()


def test_object_graph_relationship_unresolved_column_is_skipped_and_warned():
    # Only the clean single-column join survives; the stale-column, composite-AND, and ambiguous
    # joins are each dropped and recorded in relationship_warnings rather than emitted as M that
    # would point at a phantom column or pick an untrustworthy orientation.
    d = parse_tds(FEDERATED_REL_EDGECASE)
    pairs = {(r["from_table"], r["from_col"], r["to_table"], r["to_col"])
             for r in d["relationships"]}
    assert pairs == {("SALE", "REGION", "REP", "REGION")}
    warnings = d["relationship_warnings"]
    assert len(warnings) == 3
    assert any("GHOST_KEY" in w for w in warnings)         # stale / absent column
    assert any("single-column equality" in w for w in warnings)  # composite AND predicate
    assert any("ambiguous" in w for w in warnings)         # both orientations resolve differently


# -- per-connection routing (P1-B) ---------------------------------------------
def test_multi_connection_exposes_connections_map_secret_free():
    d = parse_tds(MULTI_CONN)
    assert d["named_connection_count"] == 2
    conns = d["connections"]
    assert set(conns) == {"snowflake.s", "sqlserver.t"}
    assert conns["snowflake.s"]["connection_class"] == "snowflake"
    assert conns["sqlserver.t"]["connection_class"] == "sqlserver"
    # each fact dict carries ONLY the non-secret routing whitelist -- never a credential field.
    # The odbc_* keys are non-secret (driver / DSN names, a connect-string hint, scrubbed extras,
    # the DBMS-name hint, port); the connect-string extras are scrubbed of inline creds at parse.
    allowed = {"connection_class", "server", "database", "warehouse", "http_path",
               "schema", "auth_method", "filename", "directory",
               "odbc_driver", "odbc_dsn", "odbc_connect_string_extras", "odbc_dbms_name", "port"}
    for facts in conns.values():
        assert set(facts) <= allowed
    # the actual username VALUE in the fixture must never surface (auth_method is a label, not a secret)
    assert "svc_loader" not in repr(d)
    assert all("svc_loader" not in repr(facts) for facts in conns.values())


def test_multi_connection_rebuilds_direct_by_default():
    # Default-direct policy: a >1 named-connection source whose every table routes to its own named
    # connection rebuilds in place (each relation binds to its own connector) rather than being
    # forced to the land-to-Delta fallback. The lakehouse path is an explicit option, not the default.
    from storage_mode import select_storage_mode
    decision = select_storage_mode(parse_tds(MULTI_CONN))
    assert decision["fully_supported"] is True
    assert decision["fallback"] is None


def test_multi_connection_routes_each_relation_to_its_own_connector():
    # Each relation must emit using ITS OWN named connection's connector function, not a single
    # global one: the snowflake table emits Snowflake.Databases, the sqlserver table Sql.Database.
    d = parse_tds(MULTI_CONN)
    by_name = {r["name"]: r for r in d["relations"]}
    assert "connection" in by_name["SALE"] and "connection" in by_name["DimDate"]
    sale_body = emit_m_partition_source(by_name["SALE"], d, "DirectQuery")
    date_body = emit_m_partition_source(by_name["DimDate"], d, "DirectQuery")
    assert "Snowflake.Databases" in sale_body
    assert "Sql.Database" not in sale_body
    assert 'Source = Sql.Database(#"Server", #"Database")' in date_body
    assert "Snowflake.Databases" not in date_body


def test_single_connection_ignores_per_relation_connection_byte_identical():
    # For a single-connection source the global descriptor connection wins, so a (hypothetical)
    # mismatched per-relation connection is ignored and emission stays byte-identical -- this is
    # what guarantees the established single-connector output never shifts under the new routing.
    rel = {"kind": "table", "name": "SALE", "schema": "dbo", "item": "SALE", "columns": [],
           "connection": {"connection_class": "snowflake", "server": "x",
                          "warehouse": "W", "database": "D"}}
    desc = {"connection_class": "sqlserver", "database": "Sales", "named_connection_count": 1}
    rel_plain = {k: v for k, v in rel.items() if k != "connection"}
    assert (emit_m_partition_source(rel, desc, "DirectQuery")
            == emit_m_partition_source(rel_plain, desc, "DirectQuery"))
    assert 'Sql.Database(#"Server", #"Database")' in emit_m_partition_source(rel, desc, "DirectQuery")


# -- databricks http-path attribute fallback (P4-G) ----------------------------
def test_databricks_http_path_attribute_spelling_fallback():
    # Older Tableau builds spell the warehouse path 'http-path' instead of 'v-http-path'; both
    # must resolve so the emitted Databricks.Catalogs partition still gets its HttpPath parameter.
    d = parse_tds(DATABRICKS_HTTPPATH_ALT)
    assert d["http_path"] == "/sql/1.0/warehouses/beef5678"
    body = emit_m_partition_source(d["relations"][0], d, "DirectQuery")
    assert 'Source = Databricks.Catalogs(#"Server", #"HttpPath")' in body
    params = emit_connection_parameters(d)
    assert 'expression HttpPath = "/sql/1.0/warehouses/beef5678"' in params


def test_sqlserver_cross_database_three_part_name_is_scaffold():
    # A SQL Server table fully-qualified to a DIFFERENT catalog than the connection database is a
    # cross-database reference. Sql.Database(server, database) scopes a single database, so we
    # scaffold rather than silently bind the connection's default database (which the older 2-part
    # parser avoided by rejecting the 3-part name outright).
    rel = {"kind": "table", "name": "Orders", "catalog": "OtherDb", "schema": "dbo",
           "item": "Orders", "columns": []}
    body = emit_m_partition_source(
        rel, {"connection_class": "sqlserver", "database": "Sales"}, "DirectQuery")
    assert "TODO" in body
    assert "cross-database" in body
    assert 'Source{[Schema=' not in body


def test_sqlserver_three_part_name_matching_database_emits_normally():
    # A redundant catalog qualifier equal to the connection database is dropped -> normal flat nav.
    rel = {"kind": "table", "name": "Orders", "catalog": "Sales", "schema": "dbo",
           "item": "Orders", "columns": []}
    body = emit_m_partition_source(
        rel, {"connection_class": "sqlserver", "database": "Sales"}, "DirectQuery")
    assert 'Source = Sql.Database(#"Server", #"Database")' in body
    assert 'Source{[Schema="dbo", Item="Orders"]}[Data]' in body
    assert "TODO" not in body


# Analysis Services (SSAS / MSOLAP) is already a tabular/multidimensional model -- never a naive
# M partition. It is flagged for the separate model-migration path, not emitted as upstream M.
@pytest.mark.parametrize("cls", ["msolap", "sqlserver-analysis-services"])
def test_emit_analysis_services_is_flagged_scaffold_not_m(cls):
    rel = {"kind": "table", "name": "Sales", "item": "Sales", "schema": "", "columns": []}
    body = emit_m_partition_source(rel, {"connection_class": cls}, "DirectQuery")
    assert "TODO" in body
    assert "Analysis Services" in body
    assert "model" in body.lower()
    assert "Sql.Database" not in body
    assert '(#"Server", #"Database")' not in body


def test_emit_table_none_when_no_columns():
    rel = {"kind": "table", "name": "Empty", "item": "Empty", "columns": []}
    assert emit_table_tmdl_m(rel, {"connection_class": "sqlserver"}, "Import") is None


# -- field resolver ------------------------------------------------------------
def test_m_field_resolver_resolves_caption():
    d = parse_tds(LIVE_SQLSERVER)
    resolve = build_m_field_resolver(d)
    assert resolve("Sales") == ("Orders", "Sales", "double")
    assert resolve("Quantity") == ("Orders", "Quantity", "int64")
    assert resolve("Nonexistent") is None


def test_m_field_resolver_feeds_calc_to_dax():
    from calc_to_dax import translate_tableau_calc_to_dax
    d = parse_tds(LIVE_SQLSERVER)
    resolve = build_m_field_resolver(d)
    dax, reason, _ = translate_tableau_calc_to_dax("SUM([Sales])/SUM([Quantity])", resolve)
    assert dax == "DIVIDE(SUM('Orders'[Sales]), SUM('Orders'[Quantity]))"


# -- logical-layer field resolution (case-sensitive backends) ------------------
def test_logical_fields_bridges_caption_to_uppercase_physical():
    d = parse_tds(LIVE_SNOWFLAKE_LOGICAL)
    by_caption = {f["caption"]: f for f in d["logical_fields"]}
    # The calculated column (<calculation> child) is NOT a physical binding and is excluded.
    assert "Profit Ratio" not in by_caption
    assert by_caption["Sales"]["table"] == "ORDERS"
    assert by_caption["Sales"]["physical_col"] == "SALES"
    assert by_caption["Sales"]["tmdl_type"] == "double"
    assert by_caption["Region (People)"]["table"] == "PEOPLE"


def test_m_field_resolver_logical_caption_is_case_insensitive():
    # The calc references [Sales] but the Snowflake backend column is SALES; resolve via the
    # logical layer regardless of the caption's case.
    d = parse_tds(LIVE_SNOWFLAKE_LOGICAL)
    resolve = build_m_field_resolver(d)
    assert resolve("Sales") == ("ORDERS", "SALES", "double")
    assert resolve("sales") == ("ORDERS", "SALES", "double")
    assert resolve("SALES") == ("ORDERS", "SALES", "double")
    assert resolve("Nonexistent") is None


def test_m_field_resolver_logical_disambiguates_physical_collision():
    # Physical REGION exists in both ORDERS and PEOPLE; the caption picks the right one rather
    # than failing closed on the ambiguous physical name.
    d = parse_tds(LIVE_SNOWFLAKE_LOGICAL)
    resolve = build_m_field_resolver(d)
    assert resolve("Region") == ("ORDERS", "REGION", "string")
    assert resolve("Region (People)") == ("PEOPLE", "REGION", "string")


def test_m_field_resolver_logical_feeds_conditional_agg_to_dax():
    from calc_to_dax import translate_tableau_calc_to_dax
    d = parse_tds(LIVE_SNOWFLAKE_LOGICAL)
    resolve = build_m_field_resolver(d)
    dax, _reason, _ = translate_tableau_calc_to_dax(
        'SUM(IF [Region]="West" THEN [Sales] END)', resolve)
    assert dax == "SUMX('ORDERS', IF(EXACT('ORDERS'[REGION], \"West\"), 'ORDERS'[SALES]))"


def test_logical_resolver_fails_closed_on_duplicate_map_key():
    # The same logical id is remapped to two different physical columns; the bridge must refuse
    # to bind it rather than pick whichever <map> parsed last.
    tds = LIVE_SNOWFLAKE_LOGICAL.replace(
        "<map key='[STATE]' value='[ORDERS].[STATE]' />",
        "<map key='[STATE]' value='[ORDERS].[STATE]' />\n"
        "      <map key='[SALES]' value='[ORDERS].[REGION]' />")
    d = parse_tds(tds)
    assert "Sales" not in {f["caption"] for f in d["logical_fields"]}
    assert build_m_field_resolver(d)("Sales") is None


def test_logical_resolver_fails_closed_on_case_distinct_physical():
    # ORDERS exposes both QUOTA and quota (legal on a case-sensitive backend); a logical map to
    # the case-folded name must not silently bind to the wrong one.
    extra_meta = (
        "      <metadata-record class='column'>\n"
        "        <remote-name>QUOTA</remote-name>\n"
        "        <local-name>[QUOTA]</local-name>\n"
        "        <parent-name>[ORDERS]</parent-name>\n"
        "        <local-type>real</local-type>\n"
        "      </metadata-record>\n"
        "      <metadata-record class='column'>\n"
        "        <remote-name>quota</remote-name>\n"
        "        <local-name>[quota]</local-name>\n"
        "        <parent-name>[ORDERS]</parent-name>\n"
        "        <local-type>real</local-type>\n"
        "      </metadata-record>\n")
    tds = LIVE_SNOWFLAKE_LOGICAL.replace(
        "    </metadata-records>", extra_meta + "    </metadata-records>")
    # A caption whose <cols> map points at 'Quota' (case-folds to two physical columns).
    tds = tds.replace(
        "<map key='[STATE]' value='[ORDERS].[STATE]' />",
        "<map key='[STATE]' value='[ORDERS].[STATE]' />\n"
        "      <map key='[QUOTA_CAP]' value='[ORDERS].[Quota]' />")
    tds = tds.replace(
        "  <column caption='State'",
        "  <column caption='Quota Cap' datatype='real' name='[QUOTA_CAP]' role='measure' type='quantitative' />\n"
        "  <column caption='State'")
    d = parse_tds(tds)
    # Exact 'QUOTA' / 'quota' still resolve (exact wins); the case-folded 'Quota' bridge fails closed.
    resolve = build_m_field_resolver(d)
    assert resolve("Quota Cap") is None


# -- <cols><map> pin harvest: bind an unambiguous logical pin that has NO <column> declaration -----
# A Salesforce workbook can pin a caption to exactly one physical column in <cols><map> WITHOUT ever
# declaring a matching <column caption=.. name=..>. The `_logical_fields` emit loop only walks
# <column> declarations, so that unique pin is dropped and the caption STUBS at resolve-time even
# though the mapping is unambiguous. `metadata cap_to` alone can't rescue it when the same caption
# case-folds to two physical tables (the real "Contact ID" -> Contact/Contact1 shape). Harvesting the
# unique, undeclared pin makes the caption resolve -- fail-closed (a >1-target pin is never guessed;
# a caption already handled by a <column> or metadata never shadowed).
HARVEST_UNIQUE_PIN = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='SF-Assess' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='sf' name='salesforce.1'>
        <connection class='salesforce' server='login.salesforce.com' authentication='OAuth' />
      </named-connection>
    </named-connections>
    <relation type='collection'>
      <relation connection='salesforce.1' name='Assessment' table='[Assessment]' type='table' />
      <relation connection='salesforce.1' name='Contact' table='[Contact]' type='table' />
    </relation>
    <cols>
      <map key='[Score]' value='[Assessment].[Score]' />
      <map key='[Contact ID]' value='[Contact].[Id]' />
    </cols>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Score</remote-name>
        <local-name>[Score]</local-name>
        <parent-name>[Assessment]</parent-name>
        <local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Id</remote-name>
        <local-name>[Id]</local-name>
        <parent-name>[Contact]</parent-name>
        <local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <column caption='Score' datatype='real' name='[Score]' role='measure' type='quantitative' />
</datasource>"""


# A caption that IS <column>-declared (emit loop) on Contact, PLUS a same-caption map pin to a
# DIFFERENT table (Case). Gate #2 (don't shadow a working caption/id) must keep "Contact ID" bound
# to the declared Contact.Id target and never ambiguate it with the Case pin. A third pin
# [Case Number] -> [Case].[CaseNumber] has no <column> and no metadata caption, so it SHOULD harvest
# -- proving the harvest is live in this fixture (so the "Contact ID untouched" claim is non-vacuous).
HARVEST_DECLARED_CAPTION_COLLISION = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='SF-Reg' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='sf' name='salesforce.1'>
        <connection class='salesforce' server='login.salesforce.com' authentication='OAuth' />
      </named-connection>
    </named-connections>
    <relation type='collection'>
      <relation connection='salesforce.1' name='Case' table='[Case]' type='table' />
      <relation connection='salesforce.1' name='Contact' table='[Contact]' type='table' />
    </relation>
    <cols>
      <map key='[ContactRef]' value='[Contact].[Id]' />
      <map key='[Contact ID]' value='[Case].[ContactId]' />
      <map key='[Case Number]' value='[Case].[CaseNumber]' />
    </cols>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Id</remote-name>
        <local-name>[Id]</local-name>
        <parent-name>[Contact]</parent-name>
        <local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>ContactId</remote-name>
        <local-name>[ContactId]</local-name>
        <parent-name>[Case]</parent-name>
        <local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>CaseNumber</remote-name>
        <local-name>[CaseNumber]</local-name>
        <parent-name>[Case]</parent-name>
        <local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <column caption='Contact ID' datatype='string' name='[ContactRef]' role='dimension' type='nominal' />
</datasource>"""


def test_logical_harvests_unique_map_pin_without_column():
    # RED before the harvest: [Contact ID] is pinned to exactly one physical column
    # ([Contact].[Id]) in <cols><map>, but there is NO <column caption='Contact ID' ...> for the
    # emit loop to bind, so the caption resolves to None and any calc referencing it STUBS.
    d = parse_tds(HARVEST_UNIQUE_PIN)
    resolve = build_m_field_resolver(d)
    # The declared-column path still resolves (baseline the harvest must not disturb).
    assert resolve("Score") == ("Assessment", "Score", "double")
    # The undeclared-but-unambiguous pin now binds to its single physical target.
    assert resolve("Contact ID") == ("Contact", "Id", "string")


def test_logical_harvest_does_not_ambiguate_declared_caption():
    # Regression: "Contact ID" is <column>-declared to Contact.Id AND separately map-pinned to
    # Case.ContactId. Gate #2 must keep the declared binding and refuse to add the Case target.
    d = parse_tds(HARVEST_DECLARED_CAPTION_COLLISION)
    resolve = build_m_field_resolver(d)
    # Declared caption stays bound to Contact.Id -- never shadowed/ambiguated by the Case pin.
    assert resolve("Contact ID") == ("Contact", "Id", "string")
    # A genuinely undeclared, unambiguous pin DOES harvest -> the harvest is live in this fixture,
    # so the "Contact ID untouched" assertion above is non-vacuous.
    assert resolve("Case Number") == ("Case", "CaseNumber", "string")


# -- island-scoped field resolution (Fix (1): cross-island caption collision) --------------------
# A consolidated multi-datasource workbook pools EVERY island's tables into one descriptor
# (combine_descriptors). When the SAME caption is reused across islands on DIFFERENT physical tables
# -- the real Salesforce 4-island shape -- the pooled resolver is ambiguous and returns None, so any
# calc referencing that caption STUBS. Naming the island (``datasource=``) scopes resolution to THAT
# island's tables so the caption binds to the right physical table. The scope is a strict SUPERSET:
# on an island miss it falls back to the pooled set, so it can only FIX a stub, never regress a
# previously-resolved binding.
def _island_tds(name, conn, table, *, extra=None):
    """A one-table island .tds carrying colliding ``[Category]``/``[Amount]`` captions on ``table``,
    plus an optional ``extra`` caption column unique to this island (for the superset test)."""
    extra_meta = ""
    if extra:
        extra_meta = f"""
      <metadata-record class='column'>
        <remote-name>{extra}</remote-name><local-name>[{extra}]</local-name>
        <parent-name>[{table}]</parent-name><local-type>real</local-type>
      </metadata-record>"""
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='{name}' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='c' name='{conn}'>
        <connection class='sqlserver' dbname='DB' server='srv.example.com' username='svc' />
      </named-connection>
    </named-connections>
    <relation connection='{conn}' name='{table}' table='[dbo].[{table}]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Category</remote-name><local-name>[Category]</local-name>
        <parent-name>[{table}]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Amount</remote-name><local-name>[Amount]</local-name>
        <parent-name>[{table}]</parent-name><local-type>real</local-type>
      </metadata-record>{extra_meta}
    </metadata-records>
  </connection>
</datasource>"""


def test_m_field_resolver_island_scope_disambiguates_colliding_caption():
    # Two islands reuse the SAME [Category]/[Amount] captions on DIFFERENT physical tables.
    sales = parse_tds(_island_tds("Sales Source", "sqlserver.s1", "Sales"))
    inventory = parse_tds(_island_tds("Inventory Source", "sqlserver.s2", "Inventory"))
    combined = combine_descriptors([sales, inventory],
                                   captions=["Sales Source", "Inventory Source"])

    # Pooled (datasource=None): [Amount] lives on BOTH islands -> ambiguous -> None (calc stubs).
    assert build_m_field_resolver(combined)("Amount") is None
    # Scoped to an island: the SAME caption resolves to THAT island's physical table.
    assert build_m_field_resolver(combined, datasource="Inventory Source")("Amount") \
        == ("Inventory", "Amount", "double")
    assert build_m_field_resolver(combined, datasource="Sales Source")("Amount") \
        == ("Sales", "Amount", "double")
    # A caption absent from every island still returns None under a scope (no invented binding).
    assert build_m_field_resolver(combined, datasource="Inventory Source")("Nope") is None


def test_m_field_resolver_island_scope_is_superset_falls_back_to_pooled():
    # Superset guarantee: [SalesOnly] lives on ONLY the Sales island. Scoping to the OTHER island
    # (Inventory) still resolves it -- a scoped miss falls back to the full pooled table set -- so
    # naming an island can only ADD resolutions, never drop a globally-unambiguous one.
    sales = parse_tds(_island_tds("Sales Source", "sqlserver.s1", "Sales", extra="SalesOnly"))
    inventory = parse_tds(_island_tds("Inventory Source", "sqlserver.s2", "Inventory"))
    combined = combine_descriptors([sales, inventory],
                                   captions=["Sales Source", "Inventory Source"])

    scoped_inv = build_m_field_resolver(combined, datasource="Inventory Source")
    # colliding caption -> the scoped island wins
    assert scoped_inv("Amount") == ("Inventory", "Amount", "double")
    # globally-unique caption absent from the scoped island -> pooled fallback still resolves it
    assert scoped_inv("SalesOnly") == ("Sales", "SalesOnly", "double")


def test_m_field_resolver_unscoped_is_byte_identical_when_no_collision():
    # When captions do NOT collide, an unscoped resolver over the combined descriptor behaves exactly
    # as before Fix (1): a globally-unique caption resolves to its one physical table. This locks the
    # "byte-identical when unscoped / no collision" contract.
    sales = parse_tds(_island_tds("Sales Source", "sqlserver.s1", "Sales", extra="SalesOnly"))
    # Rename Finance's captions away so nothing collides across the two islands.
    finance = parse_tds(
        _island_tds("Finance Source", "sqlserver.s2", "Finance")
        .replace("Category", "FinCategory").replace("Amount", "FinAmount"))
    combined = combine_descriptors([sales, finance],
                                   captions=["Sales Source", "Finance Source"])
    resolve = build_m_field_resolver(combined)   # unscoped
    assert resolve("SalesOnly") == ("Sales", "SalesOnly", "double")
    assert resolve("FinAmount") == ("Finance", "FinAmount", "double")
    assert resolve("FinCategory") == ("Finance", "FinCategory", "string")


# -- island-aware disambiguated captions ('<Field> (<Object>)') ----------------
# The real Salesforce "Service Delivery" shape: an object model joins Contact TWICE (Contact, Contact1),
# so BOTH copies' Id column parses to local_name='Id (Contact)' / model_name='Id'. Within one island
# the caption 'Id (Contact)' is therefore ambiguous (two tables expose it) and drops to a stub -- even
# though Tableau's own disambiguator, the '(Contact)' object token, names the intended relation exactly.
# Matching that object token to the relation literally named 'Contact' reclaims the binding.
def _disambig_island_tds(name="SD Source", conn="sqlserver.sd"):
    """A one-datasource island whose object model joins ``Contact`` twice (``Contact``/``Contact1``),
    so the disambiguated caption ``[Id (Contact)]`` lands on BOTH -> a two-table collision. ``Stage``
    lives on ``Fact``. Reproduces the exact stub the island-aware resolver must reclaim."""
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='{name}' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='c' name='{conn}'>
        <connection class='sqlserver' dbname='DB' server='srv.example.com' username='svc' />
      </named-connection>
    </named-connections>
    <relation type='collection'>
      <relation connection='{conn}' name='Fact' table='[dbo].[Fact]' type='table' />
      <relation connection='{conn}' name='Contact' table='[dbo].[Contact]' type='table' />
      <relation connection='{conn}' name='Contact1' table='[dbo].[Contact]' type='table' />
    </relation>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Stage</remote-name><local-name>[Stage]</local-name>
        <parent-name>[Fact]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Id</remote-name><local-name>[Id (Contact)]</local-name>
        <parent-name>[Contact]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Id</remote-name><local-name>[Id (Contact)]</local-name>
        <parent-name>[Contact1]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""


def test_m_field_resolver_island_aware_disambiguates_twin_join_copy():
    # Headline: the two-copy collision drops [Id (Contact)] to a stub (the shipped bug), but the
    # '(Contact)' object token names the 'Contact' relation, so island-aware resolution reclaims it.
    resolve = build_m_field_resolver(parse_tds(_disambig_island_tds()))
    assert resolve("Id (Contact)") == ("Contact", "Id", "string")
    # The bare model name is genuinely ambiguous across the two copies -> stays None (no guess).
    assert resolve("Id") is None
    # A plain (non-suffixed) caption on a single table is unaffected.
    assert resolve("Stage") == ("Fact", "Stage", "string")


def test_m_field_resolver_island_aware_reaches_through_scoped_path():
    # The scoped resolver (combined workbook, datasource=<island>) must ALSO reach island-aware
    # resolution on a base miss. The 2nd island uses DISTINCT table names so combine_descriptors does
    # NOT rename SD's Contact/Contact1 (a rename would defeat the object-token match).
    sd = parse_tds(_disambig_island_tds("SD Source", "sqlserver.sd"))
    other = parse_tds(_island_tds("Other Source", "sqlserver.o1", "Other"))
    combined = combine_descriptors([sd, other], captions=["SD Source", "Other Source"])
    resolve = build_m_field_resolver(combined, datasource="SD Source")
    assert resolve("Id (Contact)") == ("Contact", "Id", "string")


def test_m_field_resolver_island_aware_fails_closed_when_object_matches_no_relation():
    # '(Nonexistent)' names no relation -> no unique object match -> stays None (never invents a table).
    resolve = build_m_field_resolver(parse_tds(_disambig_island_tds()))
    assert resolve("Id (Nonexistent)") is None


def test_m_field_resolver_island_aware_fails_closed_when_field_absent_on_object():
    # '(Contact)' matches the Contact relation, but Contact carries no 'Stage' column -> None.
    resolve = build_m_field_resolver(parse_tds(_disambig_island_tds()))
    assert resolve("Stage (Contact)") is None


def test_m_field_resolver_island_aware_leaves_plain_caption_none():
    # A caption with no '<Field> (<Object>)' shape and no metadata match is unchanged: None.
    resolve = build_m_field_resolver(parse_tds(_disambig_island_tds()))
    assert resolve("Nope") is None


# -- bind details --------------------------------------------------------------
def test_connection_details_for_bind():
    d = parse_tds(LIVE_SQLSERVER)
    details = connection_details_for_bind(d)
    assert details["bind_type"] == "SQL"
    assert details["server"] == "myserver.database.windows.net"
    assert details["database"] == "Superstore"
    assert details["path"] == "myserver.database.windows.net;Superstore"


def test_connection_details_bind_type_for_teradata():
    details = connection_details_for_bind(
        {"connection_class": "teradata", "server": "td.example.com", "database": "ANALYTICS"})
    assert details["bind_type"] == "Teradata"
    assert details["path"] == "td.example.com;ANALYTICS"


def test_connection_details_bind_type_for_synapse():
    details = connection_details_for_bind(
        {"connection_class": "azure_sql_dw", "server": "syn.sql.azuresynapse.net", "database": "Pool"})
    assert details["bind_type"] == "SQL"
    assert details["path"] == "syn.sql.azuresynapse.net;Pool"


def test_connection_details_bind_type_for_databricks():
    details = connection_details_for_bind(
        {"connection_class": "databricks", "server": "adb.example.azuredatabricks.net", "database": "main"})
    assert details["bind_type"] == "Databricks"
    assert details["path"] == "adb.example.azuredatabricks.net;main"


def test_connection_details_bind_type_for_fabric_sql_endpoint():
    details = connection_details_for_bind(
        {"connection_class": "microsoft_fabric_sql_endpoint",
         "server": "abc.datawarehouse.fabric.microsoft.com", "database": "SalesWH"})
    assert details["bind_type"] == "SQL"
    assert details["path"] == "abc.datawarehouse.fabric.microsoft.com;SalesWH"


# -- azure_sqldb first-class path (live-validation target, pinned offline) ------
def test_parse_azure_sql_superstore_first_class_path():
    d = parse_tds(AZURE_SQL_SUPERSTORE)
    assert d["connection_class"] == "azure_sqldb"
    assert d["database"] == "Superstore"
    assert d["is_extract"] is False
    assert d["named_connection_count"] == 1
    # collection container dropped + object-model duplicates deduped -> 3 independent tables.
    assert [r["kind"] for r in d["relations"]] == ["table", "table", "table"]
    assert {r["name"] for r in d["relations"]} == {"Orders", "People", "Returns"}
    assert d["unsupported_reasons"] == []
    # credentials are never carried into the descriptor.
    blob = repr(d)
    assert "username" not in blob and "svc" not in blob


def test_azure_sqldb_full_pipeline_emits_deploy_ready_sql_database_m():
    from storage_mode import select_storage_mode
    d = parse_tds(AZURE_SQL_SUPERSTORE)

    decision = select_storage_mode(d)
    assert decision["mode"] == "DirectQuery"
    assert decision["connector"] == "Sql.Database"     # azure_sqldb speaks the SQL Server protocol
    assert decision["fully_supported"] is True
    assert decision["recommended_mode"] == "DirectQuery"
    assert decision["fallback"] is None

    by_name = {r["name"]: r for r in d["relations"]}
    orders = emit_table_tmdl_m(by_name["Orders"], d, decision["mode"])
    assert "partition Orders = m" in orders
    assert "mode: directQuery" in orders
    assert 'Source = Sql.Database(#"Server", #"Database")' in orders
    assert 'Source{[Schema="dbo", Item="Orders"]}[Data]' in orders
    assert "dataType: double" in orders   # Sales typed from Tableau metadata, not PBI inference

    # every table is deploy-ready M (no scaffold), with its own schema/item navigation.
    for name in ("Orders", "People", "Returns"):
        tmdl = emit_table_tmdl_m(by_name[name], d, decision["mode"])
        assert f'Source{{[Schema="dbo", Item="{name}"]}}[Data]' in tmdl
        assert "TODO" not in tmdl

    params = emit_connection_parameters(d)
    assert 'expression Server = "example.database.windows.net"' in params
    assert 'expression Database = "Superstore"' in params

    bind = connection_details_for_bind(d)
    assert bind["bind_type"] == "SQL"
    assert bind["path"] == "example.database.windows.net;Superstore"


# -- flat-file header reconciliation (Tableau alias vs physical Excel/CSV header) ----------------
import os as _os
import zipfile as _zipfile
from connection_to_m import (
    read_flatfile_headers,
    reconcile_flatfile_headers,
)


def _write_min_xlsx(path, sheet_name, headers):
    """Write a minimal single-sheet .xlsx (inline strings) with the given first-row headers."""
    def _col_letter(i):
        s = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(ord("A") + r) + s
        return s
    cells = "".join(
        '<c r="%s1" t="inlineStr"><is><t>%s</t></is></c>' % (_col_letter(i), h)
        for i, h in enumerate(headers)
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1">' + cells + '</row></sheetData></worksheet>'
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="%s" sheetId="1" r:id="rId1"/></sheets></workbook>' % sheet_name
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )
    with _zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook_xml)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def test_read_flatfile_headers_xlsx_by_sheet(tmp_path):
    xlsx = _os.path.join(str(tmp_path), "people.xlsx")
    _write_min_xlsx(xlsx, "People", ["Regional Manager", "Region"])
    assert read_flatfile_headers(xlsx, sheet="People") == ["Regional Manager", "Region"]


def test_read_flatfile_headers_csv_first_line(tmp_path):
    csv = _os.path.join(str(tmp_path), "orders.csv")
    with open(csv, "w", encoding="utf-8", newline="") as fh:
        fh.write("Order ID,Country/Region,Sales\n1,US,10\n")
    assert read_flatfile_headers(csv) == ["Order ID", "Country/Region", "Sales"]


def test_read_flatfile_headers_missing_returns_none(tmp_path):
    assert read_flatfile_headers(_os.path.join(str(tmp_path), "nope.csv")) is None
    assert read_flatfile_headers(None) is None


def test_reconcile_flatfile_headers_ordinal_remap(tmp_path):
    # The People table: Tableau exposes the first physical column under the ALIAS "Person" while the
    # physical header is "Regional Manager"; "Region" matches exactly. Reconcile must remap Person via
    # its ordinal (0) to the real header and leave Region alone.
    csv = _os.path.join(str(tmp_path), "people.csv")
    with open(csv, "w", encoding="utf-8", newline="") as fh:
        fh.write("Regional Manager,Region\nAlice,West\n")
    desc = {
        "flatfile_filename": "people.csv",
        "flatfile_path": csv,
        "relations": [{
            "name": "People", "kind": "table",
            "columns": [
                {"remote_name": "Person", "model_name": "Person", "ordinal": 0},
                {"remote_name": "Region", "model_name": "Region", "ordinal": 1},
            ],
        }],
    }
    res = reconcile_flatfile_headers(desc)
    assert res["mismatches"] == []
    assert [(r["from"], r["to"]) for r in res["remaps"]] == [("Person", "Regional Manager")]
    cols = {c["model_name"]: c["remote_name"] for c in desc["relations"][0]["columns"]}
    assert cols["Person"] == "Regional Manager"   # now types a header that physically exists
    assert cols["Region"] == "Region"             # exact match untouched


def test_reconcile_flatfile_headers_exact_match_is_noop(tmp_path):
    csv = _os.path.join(str(tmp_path), "orders.csv")
    with open(csv, "w", encoding="utf-8", newline="") as fh:
        fh.write("Order ID,Sales\n1,10\n")
    rels = [{
        "name": "Orders", "kind": "table",
        "columns": [
            {"remote_name": "Order ID", "model_name": "Order_ID", "ordinal": 0},
            {"remote_name": "Sales", "model_name": "Sales", "ordinal": 1},
        ],
    }]
    desc = {"flatfile_filename": "orders.csv", "flatfile_path": csv, "relations": rels}
    res = reconcile_flatfile_headers(desc)
    assert res == {"remaps": [], "mismatches": []}
    assert desc["relations"] is rels               # untouched -> same object, no copy made


def test_reconcile_flatfile_headers_unresolvable_warns_never_wrong(tmp_path):
    # A column whose alias is not a header AND whose ordinal is out of range, with MORE physical
    # headers than model columns (a hidden column), cannot be paired unambiguously -- it must NOT be
    # guessed: it is reported as a mismatch and left as-is.
    csv = _os.path.join(str(tmp_path), "t.csv")
    with open(csv, "w", encoding="utf-8", newline="") as fh:
        fh.write("Header A,Header B\nx,y\n")
    desc = {
        "flatfile_filename": "t.csv",
        "flatfile_path": csv,
        "relations": [{
            "name": "T", "kind": "table",
            "columns": [{"remote_name": "Ghost", "model_name": "Ghost", "ordinal": 9}],
        }],
    }
    res = reconcile_flatfile_headers(desc)
    assert res["remaps"] == []
    assert len(res["mismatches"]) == 1
    assert res["mismatches"][0]["source_column"] == "Ghost"
    # left untouched (never silently rebound to a wrong header)
    assert desc["relations"][0]["columns"][0]["remote_name"] == "Ghost"


def test_reconcile_flatfile_headers_robust_to_global_ordinal(tmp_path):
    # A real .tds numbers metadata-record <ordinal> datasource-GLOBALLY (People's columns are 21/22,
    # not 0/1). Reconciliation must still fix the alias via exact-match anchoring + positional pairing
    # -- NOT by using the ordinal as an absolute index (which would be out of range and miss the fix).
    csv = _os.path.join(str(tmp_path), "people.csv")
    with open(csv, "w", encoding="utf-8", newline="") as fh:
        fh.write("Regional Manager,Region\nAlice,West\n")
    desc = {
        "flatfile_filename": "people.csv",
        "flatfile_path": csv,
        "relations": [{
            "name": "People", "kind": "table",
            "columns": [
                {"remote_name": "Person", "model_name": "Person", "ordinal": 21},
                {"remote_name": "Region", "model_name": "Region", "ordinal": 22},
            ],
        }],
    }
    res = reconcile_flatfile_headers(desc)
    assert res["mismatches"] == []
    assert [(r["from"], r["to"]) for r in res["remaps"]] == [("Person", "Regional Manager")]
    cols = {c["model_name"]: c["remote_name"] for c in desc["relations"][0]["columns"]}
    assert cols["Person"] == "Regional Manager"
    assert cols["Region"] == "Region"


def test_reconcile_flatfile_headers_all_aliased_pairs_in_order(tmp_path):
    # When EVERY column is aliased (no exact anchor), equal counts still pair positionally in ordinal
    # order against the file's header order.
    csv = _os.path.join(str(tmp_path), "t.csv")
    with open(csv, "w", encoding="utf-8", newline="") as fh:
        fh.write("First,Second\n1,2\n")
    desc = {
        "flatfile_filename": "t.csv",
        "flatfile_path": csv,
        "relations": [{
            "name": "T", "kind": "table",
            "columns": [
                {"remote_name": "AliasA", "model_name": "AliasA", "ordinal": 5},
                {"remote_name": "AliasB", "model_name": "AliasB", "ordinal": 6},
            ],
        }],
    }
    res = reconcile_flatfile_headers(desc)
    assert res["mismatches"] == []
    assert [(r["from"], r["to"]) for r in res["remaps"]] == [("AliasA", "First"), ("AliasB", "Second")]


def test_reconcile_flatfile_headers_no_flatfile_is_noop():
    desc = {"relations": [{"name": "Orders", "kind": "table", "columns": []}]}
    assert reconcile_flatfile_headers(desc) == {"remaps": [], "mismatches": []}


def test_reconcile_flatfile_headers_excel_sheet_aware(tmp_path):
    xlsx = _os.path.join(str(tmp_path), "people.xlsx")
    _write_min_xlsx(xlsx, "People", ["Regional Manager", "Region"])
    desc = {
        "flatfile_filename": "people.xlsx",
        "flatfile_path": xlsx,
        "relations": [{
            "name": "People", "kind": "table", "raw_table": "[People$]",
            "columns": [
                {"remote_name": "Person", "model_name": "Person", "ordinal": 0},
                {"remote_name": "Region", "model_name": "Region", "ordinal": 1},
            ],
        }],
    }
    res = reconcile_flatfile_headers(desc)
    assert [(r["from"], r["to"]) for r in res["remaps"]] == [("Person", "Regional Manager")]


def test_flatfile_source_folder_emits_relocatable_reference(tmp_path):
    # When data is landed inside the .pbip and flatfile_source_folder is set, the emitted M must
    # reference the relocatable #"SourceFolder" parameter instead of a hard-coded absolute path.
    d = parse_tds(EXCEL_COLLECTION)
    folder = _os.path.abspath(_os.path.join(str(tmp_path), "Superstore.Data"))
    d["flatfile_source_folder"] = folder
    xlsx = _os.path.join(folder, "Sample - Superstore.xlsx")
    for rel in d["relations"]:
        rel["flatfile_path"] = xlsx
    by_name = {r["name"]: r for r in d["relations"]}
    orders_m = emit_table_tmdl_m(by_name["Orders"], d, "import")
    assert '#"SourceFolder" & "\\Sample - Superstore.xlsx"' in orders_m
    assert xlsx not in orders_m                      # the hard-coded absolute path is NOT emitted
    params = emit_connection_parameters(d)
    assert ('expression SourceFolder = "' + folder + '"') in params


def test_flatfile_without_source_folder_still_absolute(tmp_path):
    # Backward-compatible: with no flatfile_source_folder, the emitted M keeps the absolute path.
    d = parse_tds(EXCEL_COLLECTION)
    xlsx = _os.path.abspath(_os.path.join(str(tmp_path), "Sample - Superstore.xlsx"))
    for rel in d["relations"]:
        rel["flatfile_path"] = xlsx
    by_name = {r["name"]: r for r in d["relations"]}
    orders_m = emit_table_tmdl_m(by_name["Orders"], d, "import")
    assert "SourceFolder" not in orders_m
    assert "File.Contents(" in orders_m
    assert "SourceFolder" not in emit_connection_parameters(d)


# -- combine_descriptors: workbook's many embedded datasources -> ONE model (islands) ---------

def _desc(name, tables, *, relationships=None, logical=None, connections=None, **scalars):
    """Tiny descriptor shaped like parse_tds output (only the keys combine_descriptors reads)."""
    base = {
        "datasource_name": name, "connection_class": "federated", "is_extract": True,
        "relations": [{"kind": "table", "name": t, "columns": [{"model_name": "X"}]}
                      for t in tables],
        "relationships": list(relationships or []),
        "logical_fields": list(logical or []),
        "connections": dict(connections or {}),
        "relationship_warnings": [], "unsupported_reasons": [],
    }
    base.update(scalars)
    return base


def test_combine_descriptors_unions_every_table_no_drop():
    a = _desc("Sales", ["Orders", "Customers"],
              relationships=[{"from_table": "Orders", "from_col": "CustID",
                              "to_table": "Customers", "to_col": "ID"}],
              logical=[{"caption": "Sales", "table": "Orders", "physical_col": "Sales"}],
              connections={"c.a": {"class": "sqlserver"}})
    b = _desc("Finance", ["Ledger"],
              logical=[{"caption": "Amount", "table": "Ledger", "physical_col": "Amt"}],
              connections={"c.b": {"class": "snowflake"}})
    combined = combine_descriptors([a, b], captions=["Sales", "Finance"])
    names = [r["name"] for r in combined["relations"]]
    assert names == ["Orders", "Customers", "Ledger"]        # every table carried, none dropped
    assert len(combined["relationships"]) == 1
    assert {lf["table"] for lf in combined["logical_fields"]} == {"Orders", "Ledger"}
    assert combined["connections"] == {"c.a": {"class": "sqlserver"},
                                       "c.b": {"class": "snowflake"}}


def test_combine_descriptors_disambiguates_colliding_table_and_rewrites_refs():
    # Both datasources have an 'Orders' table -> the SECOND is renamed so neither table file is
    # overwritten (zero silent drops), and its relationship + logical-field refs are rewritten.
    a = _desc("Sales", ["Orders"],
              logical=[{"caption": "Sales", "table": "Orders", "physical_col": "Sales"}])
    b = _desc("Finance", ["Orders", "Ledger"],
              relationships=[{"from_table": "Orders", "from_col": "LID",
                              "to_table": "Ledger", "to_col": "ID"}],
              logical=[{"caption": "Amount", "table": "Orders", "physical_col": "Amt"}])
    combined = combine_descriptors([a, b], captions=["Sales", "Finance"])
    names = [r["name"] for r in combined["relations"]]
    assert names == ["Orders", "Orders (Finance)", "Ledger"]  # collision disambiguated, nothing lost
    rel = combined["relationships"][0]                        # renamed endpoint follows the rename
    assert rel["from_table"] == "Orders (Finance)" and rel["to_table"] == "Ledger"
    fin = [lf for lf in combined["logical_fields"] if lf["caption"] == "Amount"][0]
    assert fin["table"] == "Orders (Finance)"                # Finance's field -> renamed table
    sal = [lf for lf in combined["logical_fields"] if lf["caption"] == "Sales"][0]
    assert sal["table"] == "Orders"                          # Sales's field -> untouched original


def test_combine_descriptors_single_is_passthrough():
    a = _desc("Solo", ["Orders"])
    assert combine_descriptors([a]) is a


def test_combine_descriptors_empty_raises():
    with pytest.raises(ValueError):
        combine_descriptors([])


def test_combine_descriptors_table_map_reused_base_and_self_join():
    # Spec 6: the consolidation surfaces the base-table -> consolidated-name map it computes.
    # Assessments' Contact stays bare (primary); Service Delivery's Contact -> suffixed (reused
    # base); a same-datasource self-join alias keeps its own distinct consolidated name.
    a = _desc("Assessments", ["Contact", "Assessment"])
    b = _desc("Service Delivery", ["Contact", "Contact (Manager)", "ServiceDelivery"])
    combined = combine_descriptors([a, b], captions=["Assessments", "Service Delivery"])
    assert combined["table_map"] == {
        "Assessments||Contact": "Contact",
        "Assessments||Assessment": "Assessment",
        "Service Delivery||Contact": "Contact (Service Delivery)",
        "Service Delivery||Contact (Manager)": "Contact (Manager)",
        "Service Delivery||ServiceDelivery": "ServiceDelivery",
    }
    # every mapped value is a real emitted relation name (nothing dangles)
    rel_names = {r["name"] for r in combined["relations"]}
    assert set(combined["table_map"].values()) <= rel_names


def test_combine_descriptors_single_has_no_table_map():
    # A one-datasource passthrough carries no consolidation renaming, so no table_map key.
    a = _desc("Solo", ["Orders"])
    assert "table_map" not in combine_descriptors([a])


TWO_DATASOURCE_WB = """<?xml version='1.0' encoding='utf-8' ?>
<workbook>
  <datasources>
    <datasource caption='Sales' formatted-name='federated.sales' inline='true'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='s1' name='sqlserver.sales'>
            <connection authentication='sqlserver' class='sqlserver' dbname='SalesDB'
                        server='sql.example.com' username='svc' />
          </named-connection>
        </named-connections>
        <relation connection='sqlserver.sales' name='Orders' table='[dbo].[Orders]' type='table' />
        <metadata-records>
          <metadata-record class='column'>
            <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
            <parent-name>[Orders]</parent-name><local-type>real</local-type>
          </metadata-record>
        </metadata-records>
      </connection>
      <column caption='Sales' datatype='real' name='[Sales]' role='measure' type='quantitative' />
      <cols><map key='[Sales]' value='[Orders].[Sales]' /></cols>
    </datasource>
    <datasource caption='Finance' formatted-name='federated.finance' inline='true'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='f1' name='snowflake.fin'>
            <connection authentication='oauth' class='snowflake' dbname='FinDB'
                        server='acct.snowflakecomputing.com' warehouse='WH' />
          </named-connection>
        </named-connections>
        <relation connection='snowflake.fin' name='Ledger' table='[PUBLIC].[Ledger]' type='table' />
        <metadata-records>
          <metadata-record class='column'>
            <remote-name>Amount</remote-name><local-name>[Amount]</local-name>
            <parent-name>[Ledger]</parent-name><local-type>real</local-type>
          </metadata-record>
        </metadata-records>
      </connection>
      <column caption='Amount' datatype='real' name='[Amount]' role='measure' type='quantitative' />
      <cols><map key='[Amount]' value='[Ledger].[Amount]' /></cols>
    </datasource>
  </datasources>
</workbook>
"""


def test_combine_descriptors_from_real_two_datasource_workbook():
    from connection_to_m import workbook_datasources
    labels = [d["label"] for d in workbook_datasources(TWO_DATASOURCE_WB)]
    assert len(labels) == 2
    descriptors = [parse_tds(TWO_DATASOURCE_WB, lbl) for lbl in labels]
    combined = combine_descriptors(descriptors, captions=labels)
    names = {r["name"] for r in combined["relations"] if r.get("kind") in ("table", "custom_sql")}
    assert names == {"Orders", "Ledger"}            # both datasources' tables in ONE descriptor
    assert len(combined["connections"]) == 2        # each table still routes to its own source


def test_combine_descriptors_aggregates_hidden_prune_across_islands():
    # Each embedded datasource pruned in place before combining; the combined descriptor must carry the
    # workbook-wide prune totals so the consolidated model report can surface column_prune (fixing the
    # workbook-path telemetry gap where a physically-effective prune was under-reported as absent).
    a = _desc("Sales", ["Orders"],
              hidden_prune={"columns_emitted": 6, "columns_pruned_hidden": 4})
    b = _desc("Finance", ["Ledger"],
              hidden_prune={"columns_emitted": 5, "columns_pruned_hidden": 90})
    combined = combine_descriptors([a, b], captions=["Sales", "Finance"])
    assert combined["hidden_prune"] == {"columns_emitted": 11, "columns_pruned_hidden": 94}


def test_combine_descriptors_hidden_prune_sums_only_pruned_islands():
    # Mixed workbook: one island hid columns, the other (e.g. an all-Superstore datasource) did not.
    # Sum comes only from the island that pruned; the un-pruned island contributes nothing.
    a = _desc("SF", ["Case"],
              hidden_prune={"columns_emitted": 6, "columns_pruned_hidden": 4})
    b = _desc("Superstore", ["Orders"])            # no hidden_prune key at all
    combined = combine_descriptors([a, b], captions=["SF", "Superstore"])
    assert combined["hidden_prune"] == {"columns_emitted": 6, "columns_pruned_hidden": 4}


def test_combine_descriptors_omits_hidden_prune_when_no_island_pruned():
    # A workbook where nothing was hidden must stay byte-identical: no hidden_prune key on the combined
    # descriptor (so column_prune stays None, exactly as before this fix).
    a = _desc("Sales", ["Orders"])
    b = _desc("Finance", ["Ledger"])
    combined = combine_descriptors([a, b], captions=["Sales", "Finance"])
    assert "hidden_prune" not in combined


# -- hidden-column prune (connector-agnostic) ---------------------------------
# A Salesforce-style datasource exposes the full physical schema but HIDES ~90% of it via logical
# <column hidden='true'> elements. The prune drops the unreferenced hidden columns while CARVING OUT
# (keeping, flagged isHidden) the load-bearing ones -- relationship join keys and calc-referenced
# fields (incl. one resolved only through the <cols><map> lid bridge, not the metadata-name index).
HIDDEN_HEAVY_TDS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='SF' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='sf' name='salesforce.s'>
        <connection authentication='OAuth' class='salesforce' server='login.salesforce.com' />
      </named-connection>
    </named-connections>
    <relation type='collection'>
      <relation connection='salesforce.s' name='Case' table='[Case]' type='table' />
      <relation connection='salesforce.s' name='Contact' table='[Contact]' type='table' />
    </relation>
    <cols>
      <map key='[CaseNumber]' value='[Case].[CaseNumber]' />
      <map key='[Status]' value='[Case].[Status]' />
      <map key='[ContactId]' value='[Case].[ContactId]' />
      <map key='[Priority]' value='[Case].[Priority]' />
      <map key='[Priority Rank]' value='[Case].[Internal_Flag]' />
      <map key='[Junk_A]' value='[Case].[Junk_A]' />
      <map key='[Junk_B]' value='[Case].[Junk_B]' />
      <map key='[Id]' value='[Contact].[Id]' />
      <map key='[FullName]' value='[Contact].[FullName]' />
      <map key='[SecretEmail]' value='[Contact].[SecretEmail]' />
    </cols>
    <object-graph>
      <objects>
        <object caption='Case' id='Case (Case)_A1'>
          <properties>
            <relation connection='salesforce.s' name='Case' table='[Case]' type='table' />
          </properties>
        </object>
        <object caption='Contact' id='Contact (Contact)_B2'>
          <properties>
            <relation connection='salesforce.s' name='Contact' table='[Contact]' type='table' />
          </properties>
        </object>
      </objects>
      <relationships>
        <relationship>
          <expression op='='>
            <expression op='[ContactId]' />
            <expression op='[Id]' />
          </expression>
          <first-end-point object-id='Case (Case)_A1' />
          <second-end-point object-id='Contact (Contact)_B2' />
        </relationship>
      </relationships>
    </object-graph>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>CaseNumber</remote-name><local-name>[CaseNumber]</local-name>
        <parent-name>[Case]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Status</remote-name><local-name>[Status]</local-name>
        <parent-name>[Case]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>ContactId</remote-name><local-name>[ContactId]</local-name>
        <parent-name>[Case]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Priority</remote-name><local-name>[Priority]</local-name>
        <parent-name>[Case]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Internal_Flag</remote-name><local-name>[Internal_Flag]</local-name>
        <parent-name>[Case]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Junk_A</remote-name><local-name>[Junk_A]</local-name>
        <parent-name>[Case]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Junk_B</remote-name><local-name>[Junk_B]</local-name>
        <parent-name>[Case]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Id</remote-name><local-name>[Id]</local-name>
        <parent-name>[Contact]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>FullName</remote-name><local-name>[FullName]</local-name>
        <parent-name>[Contact]</parent-name><local-type>string</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>SecretEmail</remote-name><local-name>[SecretEmail]</local-name>
        <parent-name>[Contact]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
  <column caption='Case Health' datatype='integer' name='[Calculation_1]' role='measure' type='quantitative'>
    <calculation class='tableau' formula='IF [Priority Rank] = "High" THEN 1 ELSE 0 END' />
  </column>
  <column datatype='string' hidden='true' name='[ContactId]' />
  <column datatype='string' hidden='true' name='[Priority]' />
  <column datatype='string' hidden='true' name='[Priority Rank]' />
  <column datatype='string' hidden='true' name='[Junk_A]' />
  <column datatype='string' hidden='true' name='[Junk_B]' />
  <column datatype='string' hidden='true' name='[Id]' />
  <column datatype='string' hidden='true' name='[SecretEmail]' />
</datasource>"""


def _prune_cols(desc, table):
    """{model_name: is_hidden(bool)} for each emitted column of a table (by display name)."""
    rel = next(r for r in desc["relations"] if r.get("name") == table)
    return {c["model_name"]: bool(c.get("is_hidden")) for c in (rel.get("columns") or [])}


def test_hidden_prune_drops_unreferenced_keeps_loadbearing():
    desc = parse_tds(HIDDEN_HEAVY_TDS)
    assert desc["hidden_prune"] == {"columns_emitted": 6, "columns_pruned_hidden": 4}
    case = _prune_cols(desc, "Case")
    # visible physical columns kept, never flagged
    assert case["CaseNumber"] is False
    assert case["Status"] is False
    # unreferenced hidden columns dropped entirely
    assert "Priority" not in case
    assert "Junk_A" not in case
    assert "Junk_B" not in case
    # hidden JOIN KEY + hidden CALC-REF kept, flagged isHidden
    assert case["ContactId"] is True
    assert case["Internal_Flag"] is True
    contact = _prune_cols(desc, "Contact")
    assert contact["FullName"] is False
    assert contact["Id"] is True             # hidden join key kept
    assert "SecretEmail" not in contact      # unreferenced hidden dropped


def test_hidden_prune_carves_calc_ref_via_cols_map_bridge():
    # 'Case Health' references [Priority Rank] -- a cols-map-only lid mapping to [Case].[Internal_Flag].
    # The metadata-name index has no 'Priority Rank' entry, so ONLY the <cols><map> bridge can carve
    # Internal_Flag out. If the bridge regressed, Internal_Flag would be dropped and the calc dangle.
    desc = parse_tds(HIDDEN_HEAVY_TDS)
    case = _prune_cols(desc, "Case")
    assert "Internal_Flag" in case and case["Internal_Flag"] is True


def test_hidden_prune_emits_isHidden_in_tmdl():
    desc = parse_tds(HIDDEN_HEAVY_TDS)
    case_rel = next(r for r in desc["relations"] if r.get("name") == "Case")
    tmdl = emit_table_tmdl_m(case_rel, desc, "import")
    # carved-out hidden columns emit isHidden; a visible column does not
    assert "column ContactId\n\t\tdataType: string\n\t\tisHidden" in tmdl
    assert "column Internal_Flag\n\t\tdataType: string\n\t\tisHidden" in tmdl
    assert "column CaseNumber\n\t\tdataType: string\n\t\tisHidden" not in tmdl


def test_hidden_prune_connector_agnostic():
    # The prune keys off the generic (parent, model_name) identity space and NEVER branches on the
    # connector class. A SQL Server datasource with the identical hidden-heavy shape prunes identically
    # to the Salesforce one -- the fix scales across all connectors, not just Salesforce.
    sql = (HIDDEN_HEAVY_TDS
           .replace("class='salesforce' server='login.salesforce.com'",
                    "class='sqlserver' server='db.example.com' dbname='SF'")
           .replace("authentication='OAuth'", "authentication='sqlserver'"))
    desc = parse_tds(sql)
    assert desc["hidden_prune"] == {"columns_emitted": 6, "columns_pruned_hidden": 4}
    case = _prune_cols(desc, "Case")
    assert case["ContactId"] is True and case["Internal_Flag"] is True
    assert "Junk_A" not in case and "Junk_B" not in case
    assert _prune_cols(desc, "Contact")["Id"] is True


def test_hidden_prune_noop_when_nothing_hidden():
    # With no hidden <column> elements the prune is a byte-identical no-op: the descriptor carries
    # no hidden_prune record and every physical column emits, unflagged (Superstore-fixture parity).
    import re as _re
    no_hidden = _re.sub(r"\s*<column datatype='string' hidden='true'[^>]*/>", "", HIDDEN_HEAVY_TDS)
    desc = parse_tds(no_hidden)
    assert desc.get("hidden_prune") is None
    case = _prune_cols(desc, "Case")
    assert len(case) == 7 and not any(case.values())
    contact = _prune_cols(desc, "Contact")
    assert len(contact) == 3 and not any(contact.values())
