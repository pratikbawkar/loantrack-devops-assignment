-- 002: seed loans.
-- Each row is inserted only if an identical loan (same borrower, amount, city)
-- is not already present, so running this file again adds no duplicates.
-- The schema has no natural unique key, hence NOT EXISTS instead of ON CONFLICT.

INSERT INTO loans (borrower_name, loan_amount, property_city, status)
SELECT seed.borrower_name, seed.loan_amount, seed.property_city, seed.status
FROM (
    VALUES
        (1, 'Aarav Sharma',   4500000.00, 'Pune',      'APPROVED'),
        (2, 'Priya Nair',     2750000.50, 'Kochi',     'PENDING'),
        (3, 'Rohan Mehta',    8200000.00, 'Mumbai',    'UNDER_REVIEW'),
        (4, 'Sneha Kulkarni', 1980000.75, 'Nagpur',    'REJECTED'),
        (5, 'Vikram Reddy',   6400000.00, 'Hyderabad', 'DISBURSED'),
        (6, 'Ananya Iyer',    3325000.25, 'Chennai',   'PENDING')
) AS seed (seed_order, borrower_name, loan_amount, property_city, status)
WHERE NOT EXISTS (
    SELECT 1
    FROM loans existing
    WHERE existing.borrower_name = seed.borrower_name
      AND existing.loan_amount   = seed.loan_amount
      -- IS NOT DISTINCT FROM also matches when both cities are NULL.
      AND existing.property_city IS NOT DISTINCT FROM seed.property_city
)
-- seed_order exists only in this query (not in the table): without it the
-- anti-join may return rows in any order, so ids would not follow the list.
ORDER BY seed.seed_order;
