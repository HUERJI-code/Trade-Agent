# 教学数据库结构报告

检查时间：2026-09-09T14:14:12（本机时间）。

来源：用户提供的 Kaggle Global Electronics Retailers 数据包。以下结构与统计直接读取本机 MySQL 的 trade_agent_demo。

使用 trade_agent_readonly，只展示字段、约束和聚合统计，不展示客户个人记录。

## 表结构与源字段映射

### customers

来源 Customers.csv；记录数 15,266；主键：customer_key。

| 数据库字段 | 原始字段 | 类型 | 可为空 | 客户个人信息候选 |
|---|---|---|---|---|
| customer_key | CustomerKey | INTEGER | 否 |  |
| gender | Gender | VARCHAR(20) COLLATE "utf8mb4_0900_as_cs" | 否 |  |
| customer_name | Name | VARCHAR(255) COLLATE "utf8mb4_0900_as_cs" | 否 | 是 |
| city | City | VARCHAR(255) COLLATE "utf8mb4_0900_as_cs" | 否 | 是 |
| state_code | State Code | VARCHAR(50) COLLATE "utf8mb4_0900_as_cs" | 否 | 是 |
| state | State | VARCHAR(255) COLLATE "utf8mb4_0900_as_cs" | 否 | 是 |
| zip_code | Zip Code | VARCHAR(50) COLLATE "utf8mb4_0900_as_cs" | 否 | 是 |
| country | Country | VARCHAR(100) COLLATE "utf8mb4_0900_as_cs" | 否 |  |
| continent | Continent | VARCHAR(100) COLLATE "utf8mb4_0900_as_cs" | 否 |  |
| birthday | Birthday | DATE | 否 | 是 |


### products

来源 Products.csv；记录数 2,517；主键：product_key。

| 数据库字段 | 原始字段 | 类型 | 可为空 | 客户个人信息候选 |
|---|---|---|---|---|
| product_key | ProductKey | INTEGER | 否 |  |
| product_name | Product Name | VARCHAR(255) COLLATE "utf8mb4_0900_as_cs" | 否 |  |
| brand | Brand | VARCHAR(100) COLLATE "utf8mb4_0900_as_cs" | 否 |  |
| color | Color | VARCHAR(50) COLLATE "utf8mb4_0900_as_cs" | 否 |  |
| unit_cost_usd | Unit Cost USD | DECIMAL(12, 2) | 否 |  |
| unit_price_usd | Unit Price USD | DECIMAL(12, 2) | 否 |  |
| subcategory_key | SubcategoryKey | INTEGER | 否 |  |
| subcategory | Subcategory | VARCHAR(255) COLLATE "utf8mb4_0900_as_cs" | 否 |  |
| category_key | CategoryKey | INTEGER | 否 |  |
| category | Category | VARCHAR(255) COLLATE "utf8mb4_0900_as_cs" | 否 |  |


### stores

来源 Stores.csv；记录数 67；主键：store_key。

| 数据库字段 | 原始字段 | 类型 | 可为空 | 客户个人信息候选 |
|---|---|---|---|---|
| store_key | StoreKey | INTEGER | 否 |  |
| country | Country | VARCHAR(100) COLLATE "utf8mb4_0900_as_cs" | 否 |  |
| state | State | VARCHAR(255) COLLATE "utf8mb4_0900_as_cs" | 否 |  |
| square_meters | Square Meters | INTEGER | 是 |  |
| open_date | Open Date | DATE | 否 |  |


### exchange_rates

来源 Exchange_Rates.csv；记录数 11,215；主键：rate_date, currency_code。

| 数据库字段 | 原始字段 | 类型 | 可为空 | 客户个人信息候选 |
|---|---|---|---|---|
| rate_date | Date | DATE | 否 |  |
| currency_code | Currency | VARCHAR(3) COLLATE "utf8mb4_0900_as_cs" | 否 |  |
| exchange_rate | Exchange | DECIMAL(18, 8) | 否 |  |


### sales

来源 Sales.csv；记录数 62,884；主键：order_number, line_item。

| 数据库字段 | 原始字段 | 类型 | 可为空 | 客户个人信息候选 |
|---|---|---|---|---|
| order_number | Order Number | INTEGER | 否 |  |
| line_item | Line Item | INTEGER | 否 |  |
| order_date | Order Date | DATE | 否 |  |
| delivery_date | Delivery Date | DATE | 是 |  |
| customer_key | CustomerKey | INTEGER | 否 |  |
| store_key | StoreKey | INTEGER | 否 |  |
| product_key | ProductKey | INTEGER | 否 |  |
| quantity | Quantity | INTEGER | 否 |  |
| currency_code | Currency Code | VARCHAR(3) COLLATE "utf8mb4_0900_as_cs" | 否 |  |

- 外键：customer_key → customers(customer_key)。
- 外键：product_key → products(product_key)。
- 外键：store_key → stores(store_key)。
- 外键：order_date, currency_code → exchange_rates(rate_date, currency_code)。

- 订单日期范围：2016-01-01 / 2021-02-20 / 26326。
- 交付日期缺失条数：49719。
- 门店面积缺失条数：1。
- 汇率日期范围：2015-01-01 / 2021-02-20。
- 客户国家：Australia, Canada, France, Germany, Italy, Netherlands, United Kingdom, United States。
- 订单币种：AUD, CAD, EUR, GBP, USD。

## 分析边界与待确认口径

- 订单明细主键为订单号与行号，订单数需去重，不能用明细行数代替。
- 产品表金额单位为 USD。unit_price_usd 是标价；销售表没有实际成交价。数量乘标价是已确认的教学估算口径，展示时必须标注估算。
- unit_cost_usd 为单位生产成本；缺少历史成本、折扣、税费、运费与退货记录。无法直接确认净利润或成本变动原因。
- 已确认按产品 USD 标价计算，不按订单币种再次折算；汇率表已导入但当前指标未启用换汇。
- 日期是 2016 至 2021 年的历史样例；2026 年等区间不能编造答案。
- 国家筛选已确认使用客户国家，下单日期用于时间筛选；交付日期为空不等于订单未完成。
- 客户姓名、生日、详细位置等字段已由应用工具白名单屏蔽；数据库只读账号本身仍可读五张完整教学表。
- 本报告仅核对数据结构与聚合统计；当前应用实现进度请查看 README.md 与各阶段验收报告。
