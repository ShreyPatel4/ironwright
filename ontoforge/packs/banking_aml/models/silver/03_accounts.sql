select acct_nbr, cust_nbr, product, strptime(open_date, '%Y%m%d')::date as open_date, status
from bronze_account_master
