"""Ocea resident API protocol constants."""

# Azure AD B2C
B2C_TENANT = "osbespaceresident"
B2C_CLIENT_ID = "1cacfb15-0b3c-42cc-a662-736e4737e7d9"
B2C_SCOPE = (
    "https://osbespaceresident.onmicrosoft.com/"
    "app-imago-espace-resident-back-prod/user_impersonation "
    "openid profile offline_access"
)
B2C_REDIRECT_URI = "https://espace-resident.ocea-sb.com"

B2C_BASE = f"https://{B2C_TENANT}.b2clogin.com"
B2C_TENANT_PATH = f"{B2C_BASE}/{B2C_TENANT}.onmicrosoft.com"
B2C_AUTHORIZE = f"{B2C_TENANT_PATH}/b2c_1a_signup_signin/oauth2/v2.0/authorize"
B2C_TOKEN = f"{B2C_TENANT_PATH}/b2c_1a_signup_signin/oauth2/v2.0/token"

API_BASE = "https://espace-resident-api.ocea-sb.com"
