class DataQualityQueryBuilder:
    def __init__(self):
        # Map each rule_name to its specific SQL WHERE condition
        self.rules_mapping = {
            # --- Common / Visibility Rules ---
            "timestamp_present": "time_stamp IS NULL",
            "platform_constant": "platform != '{platform}'", 
            "platform_code_present": "platform_code IS NULL",
            "code_present": "platform_code IS NULL",
            "name_present": "(product_name IS NULL OR LENGTH(TRIM(product_name)) < 5)",
            "sp_positive": "sp < 1",
            "sp_not_above_mrp": "(sp IS NOT NULL AND mrp IS NOT NULL) AND sp > mrp",
            "rating_bounds": "(rating < 1 OR rating > 5)",
            "num_of_rating_not_null": "rating IS NOT NULL AND num_of_rating IS NULL",
            "rank_present": "absolute_rank IS NULL",
            "relative_rank_not_null": "relative_rank IS NULL",
            "relative_rank_must_not_null": "relative_rank IS NULL",
            "campaign_id_must_be_null": "campaign_id IS NOT NULL",
            "campaign_id_required_for_ads": "product_type IN ('SB', 'SBV', 'SP') AND campaign_id IS NULL",
            "campaign_id_not_null": "product_type IN ('SP', 'SB', 'SD', 'SBV') AND campaign_id IS NULL",
            
            # --- Flipkart Specific ---
            "fsn_format": "platform_code NOT REGEXP '^[A-Z0-9]{16}$'",
            
            # --- OSA Specific ---
            "stock_status_valid": "(stock_status NOT IN ('In Stock', 'Out of Stock', 'Currently unavailable') OR stock_status IS NULL)",
            "sp_null_when_instock": "stock_status = 'In Stock' AND sp IS NULL",
            "delivery_days_sane": "(delivery_days < 0 OR delivery_days > 60)",
            "asin_format": "platform_code NOT REGEXP '^B0[A-Z0-9]{8}$'",
        }

    def retrieve_query(self, table_name, rule_name, date, time_range, platform="Amazon"):
        """
        Retrieves the dynamically generated SQL query.
        
        :param table_name: e.g., 't_visibility_hourly' or 't_osa_hourly'
        :param rule_name: The name of the rule to trigger (e.g., 'sp_not_above_mrp')
        :param date: The date string, e.g., '2026-07-16'
        :param time_range: A tuple of (start_hour, end_hour), e.g., (1, 4)
        :param platform: The platform string (defaults to 'Amazon')
        """
        start_hour, end_hour = time_range
        
        # 1. Handle CTE edge case (image_diversity requires a complex layout)
        if rule_name == "image_diversity":
            return f"""WITH CrawlStats AS (
    SELECT 
        keyword,
        time_stamp,
        product_image_url,
        COUNT(*) AS image_occurrences,
        SUM(COUNT(*)) OVER (PARTITION BY keyword, time_stamp) AS total_crawl_rows
    FROM lakehouse.silver.{table_name}
    WHERE platform = '{platform}' 
      AND date_stamp = DATE '{date}' 
      AND hour_stamp BETWEEN {start_hour} AND {end_hour}
    GROUP BY keyword, time_stamp, product_image_url
)
SELECT 
    keyword,
    time_stamp,
    product_image_url,
    image_occurrences,
    total_crawl_rows,
    (CAST(image_occurrences AS DOUBLE) / total_crawl_rows) AS image_share
FROM CrawlStats
WHERE (CAST(image_occurrences AS DOUBLE) / total_crawl_rows) > 0.5;"""

        # 2. Validate rule exists
        if rule_name not in self.rules_mapping:
            raise ValueError(f"Rule '{rule_name}' is not recognized.")
            
        # 3. Format the specific condition (handles cases like platform_constant)
        condition = self.rules_mapping[rule_name].format(platform=platform)
        
        # 4. Handle specific SELECT variations (you used COUNT(*) for this specific rule in visibility)
        if rule_name == "num_of_rating_not_null" and table_name == "t_visibility_hourly":
            select_clause = "SELECT count(*)"
        else:
            select_clause = "SELECT *"
            
        # 5. Build and return the final query
        query = f"""{select_clause} 
FROM lakehouse.silver.{table_name}
WHERE platform = '{platform}' 
  AND date_stamp = DATE '{date}' 
  AND hour_stamp BETWEEN {start_hour} AND {end_hour}
  AND {condition};"""
        
        return query