import pulumi
import pulumi_aws as aws
import pulumi_aws_native as aws_native # For some newer API Gateway v2 resources if needed
import json
import os

# --- Configuration ---
# You can get the Finnhub API Key from Pulumi config or directly from environment variables
# For Pulumi config:
# pulumi config set finnhubApiKey YOUR_KEY --secret
# config = pulumi.Config()
# finnhub_api_key = config.require_secret("finnhubApiKey")
# For this example, we'll assume it's set as an environment variable for the Pulumi process
# or you can hardcode it during testing (not recommended for production)

# --- IAM Role for Lambda ---
lambda_role = aws.iam.Role("stockApiLambdaRole",
    assume_role_policy=json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Action": "sts:AssumeRole",
            "Effect": "Allow",
            "Principal": {
                "Service": "lambda.amazonaws.com",
            },
        }],
    }),
    tags={
        "Name": "stock-api-lambda-role",
    })

# Attach the basic Lambda execution policy (for CloudWatch Logs)
log_policy_attachment = aws.iam.RolePolicyAttachment("stockApiLambdaLogPolicyAttachment",
    role=lambda_role.name,
    policy_arn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")

# --- DynamoDB Table ---
dynamodb_table = aws.dynamodb.Table("brokerDataTable",
    attributes=[
        aws.dynamodb.TableAttributeArgs(
            name="id", # Primary key attribute name
            type="S",  # S for String, N for Number, B for Binary
        ),
        aws.dynamodb.TableAttributeArgs(
            name="symbol", # Sort key attribute name
            type="S",     # S for String
        ),
    ],
    hash_key="id", # The name of the attribute to use as the hash key (partition key)
    range_key="symbol", # The name of the attribute to use as the range key (sort key)
    billing_mode="PROVISIONED", # Changed from PAY_PER_REQUEST
    read_capacity=5,  # Free tier eligible (e.g., 5 RCUs)
    write_capacity=5, # Free tier eligible (e.g., 5 WCUs)
    tags={
        "Name": "broker-data-table",
        "Project": "Bro-Ker",
    })

# --- IAM Policy for Lambda to access DynamoDB ---
dynamodb_lambda_policy = aws.iam.Policy("stockApiLambdaDynamoDbPolicy",
    description="IAM policy for Lambda to read/write from the Broker DynamoDB table",
    policy=dynamodb_table.arn.apply(lambda arn: json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Scan", "dynamodb:Query"],
            "Effect": "Allow",
            "Resource": arn, # Grant access to the specific table
        }]
    })))

# Attach the DynamoDB access policy to the Lambda role
dynamodb_policy_attachment = aws.iam.RolePolicyAttachment("stockApiLambdaDynamoDbAttachment",
    role=lambda_role.name,
    policy_arn=dynamodb_lambda_policy.arn)

# --- Lambda Function ---
# Create a Lambda layer for dependencies if you have many or large ones.
# For a single small dependency like finnhub-python, zipping it with the code is often fine.
# However, best practice for cleaner deployments is often a layer.

# 1. Create the deployment package (zip file)
# Pulumi can automatically zip a directory.
lambda_asset_archive = pulumi.FileArchive("../broker-backend/deploy.zip") # Points to the directory with your lambda_function.py and its requirements.txt

stock_api_lambda = aws.lambda_.Function("BrokerBackendFunction",
    role=lambda_role.arn,
    runtime="python3.9",  
    handler="broker.lambda_handler", # filename.handler_function
    code=lambda_asset_archive,
    timeout=10,  # Seconds, adjust as needed
    memory_size=128, # MB
    environment=aws.lambda_.FunctionEnvironmentArgs(
        variables={
            "FINNHUB_API_KEY": "null",
            "COGNITO_TOKEN_ENDPOINT": "null",
            "COGNITO_CLIENT_ID": "null",
            "COGNITO_CLIENT_SECRET": "null",
            "DYNAMODB_TABLE_NAME": dynamodb_table.name, # Pass table name to Lambda
            # Add any other environment variables your Lambda needs
        },
    ),
    tags={
        "Name": "stock-api-function",
        "Project": "Bro-Ker",
    },
    opts=pulumi.ResourceOptions(ignore_changes=["code", "environment"])
    )  # Ignore changes to the code, so it doesn't trigger redeployments unnecessarily
    

## NEW ## --- Cognito User Pool for Authentication ---

# 1. Create the User Pool
user_pool = aws.cognito.UserPool("brokerUserPool",
    name="BrokerAppUserPool",
    # Configure password policy, etc. as needed
    password_policy=aws.cognito.UserPoolPasswordPolicyArgs(
        minimum_length=8,
        require_lowercase=True,
        require_numbers=True,
        require_symbols=True,
        require_uppercase=True,
    ),
    # Require email for user accounts and auto-verify it
    auto_verified_attributes=["email"],
    tags={
        "Name": "broker-user-pool",
    })

# 2. Create the User Pool Client
# This is what your frontend application will use to interact with Cognito.
user_pool_client = aws.cognito.UserPoolClient("brokerUserPoolClient",
    name="BrokerAppClient",
    user_pool_id=user_pool.id,
    # Set to False because client-side apps (like a browser SPA) can't securely store a secret.
    generate_secret=True,
    # Define authentication flows. USER_PASSWORD_AUTH is for direct user/pass login.
    # REFRESH_TOKEN_AUTH allows the app to get new tokens without the user logging in again.
    explicit_auth_flows=[
        "ALLOW_USER_PASSWORD_AUTH",
        "ALLOW_REFRESH_TOKEN_AUTH",
        "ALLOW_ADMIN_USER_PASSWORD_AUTH"
    ],
    # You would configure these with your actual frontend URLs
    callback_urls=["https://bro-ker.com/callback"],
    logout_urls=["https://bro-ker.com/logout"],
    )

# --- API Gateway (HTTP API v2) ---
# Create an HTTP API
http_api = aws_native.apigatewayv2.Api("stockHttpApi",
    name="StockBrokerHttpApi",
    protocol_type="HTTP",
    description="API for Bro-Ker stock information",
    #target=stock_api_lambda.invoke_arn,  # Directly set the Lambda function ARN as the target
    # Corrected CORS Configuration using a dictionary:
    cors_configuration={ 
        "allowOrigins": ["https://bro-ker.com", "http://localhost:5500", "http://127.0.0.1:5500"],
        "allowMethods": ["GET", "OPTIONS","POST", "PUT", "DELETE"],
        "allowHeaders": ["Content-Type", "X-Amz-Date", "Authorization", "X-Api-Key", "X-Amz-Security-Token"],
        "maxAge": 300,
    },
    tags={
        "Name": "stock-broker-http-api",
    })

# Create a default stage for the HTTP API
# HTTP APIs automatically create a $default stage, but explicitly defining it
# can sometimes be useful for adding stage variables or other configurations.
# For basic deployment, the auto-created one is sufficient. We don't need to explicitly
# define the $default stage resource unless we need to configure specific stage settings
# like logging, tracing, or stage variables. The API Mapping resource below will
# correctly reference the auto-created $default stage.

domain_name='api.bro-ker.com'

# Create a custom domain name for the API Gateway
# Ensure you have a validated ACM certificate in the same region
api_custom_domain = aws_native.apigatewayv2.DomainName("bro-kerApiCustomDomain",
    domain_name=domain_name,
    domain_name_configurations=[aws_native.apigatewayv2.DomainNameConfigurationArgs(
        certificate_arn='arn:aws:acm:ap-southeast-1:724596670824:certificate/3f0a93bf-28f1-4a49-93e2-59e00d7e08e8',
        endpoint_type="REGIONAL", # Or "EDGE" if using CloudFront (more complex setup)
        # security_policy="TLS_1_2", # Optional, defaults to TLS_1_2 for REGIONAL
    )],
    tags={
        "Name": domain_name,
    }
)

# --- API Gateway API Mapping ---
# This maps the $default stage of your HTTP API to the custom domain.
# HTTP APIs have an auto-created $default stage.
api_mapping = aws_native.apigatewayv2.ApiMapping("bro-ker-apiMapping",
    api_id=http_api.id,
    domain_name=api_custom_domain.domain_name, # Use the domain_name from the custom domain resource
    stage="$default"
    #api_mapping_key='v1' #not so sophisticated yet
)

jwt_authorizer = aws_native.apigatewayv2.Authorizer("brokerJwtAuthorizer",
    api_id=http_api.id,
    name="CognitoJwtAuthorizer",
    authorizer_type="JWT",
    # Where to find the token in the incoming request
    identity_source=["$request.header.Authorization"],
    # Configure the authorizer to use our Cognito User Pool
    jwt_configuration=aws_native.apigatewayv2.AuthorizerJwtConfigurationArgs(
        # The 'audience' must match the 'client ID' of your User Pool Client
        audience=[user_pool_client.id],
        # The 'issuer' URL for your User Pool
        issuer=user_pool.id.apply(
            lambda pool_id: f"https://cognito-idp.{aws.get_region().name}.amazonaws.com/{pool_id}"
        ),
    ))



# Create Lambda integration for API Gateway
lambda_integration = aws_native.apigatewayv2.Integration("bro-ker-backend",
    api_id=http_api.id,
    integration_type="AWS_PROXY",
    integration_uri=stock_api_lambda.invoke_arn,
    payload_format_version="2.0",
    # --- Request Parameter Mapping ---
    # This section demonstrates how to map request data to the integration.
    # For AWS_PROXY with Lambda, these mapped parameters become part of the 'event'
    # object received by the Lambda function.
    request_parameters={
        # This maps the 'sub' (subject/user ID) claim from the JWT authorizer
        # Your Lambda function can then access this via event['queryStringParameters']['userIdAuth'].
        "overwrite:querystring.userIdAuth": "$context.authorizer.claims.sub",

        # You could also map other things, e.g., a static value or another request part:
        # "overwrite:querystring.staticValue": "'bro-ker-api-source'", # Static string value
        # "overwrite:header.X-Mapped-Header": "$request.header.Some-Client-Header" # Map a client header
    },
    # If updating the integration (e.g., by adding request_parameters) fails due to
    # "required key [IntegrationType] not found", it might be a provider issue
    # with how it handles updates. Forcing a replacement when request_parameters
    # changes can be a workaround.
    opts=pulumi.ResourceOptions(
        replace_on_changes=["request_parameters"]
    ))


# A helper function to format the target ARN correctly
def format_integration_target(integration_id_output):
    return integration_id_output.apply(
        lambda composite_id: f"integrations/{composite_id.split('|')[1]}" if isinstance(composite_id, str) and '|' in composite_id else "integrations/error-invalid-id-format"
    )


# --- API Routes with Authentication ---
# Create routes for the API
auth_profile_route = aws_native.apigatewayv2.Route("authProfileRoute",
    api_id=http_api.id,
    route_key="GET /api/auth", # A new path for authenticated users
    target=format_integration_target(lambda_integration.id),
    opts=pulumi.ResourceOptions(
        replace_on_changes=["target"],
        delete_before_replace=True
    )
)

# ## MODIFIED ## --- Update existing routes to be protected ---
quotes_route = aws_native.apigatewayv2.Route("stockQuotesRoute",
    api_id=http_api.id,
    route_key="GET /api/stock-quotes",
    target=format_integration_target(lambda_integration.id),
    authorization_type="JWT", # Protect this route
    authorizer_id=jwt_authorizer.authorizer_id, # Link to the authorizer (use .authorizer_id for aws_native)
    opts=pulumi.ResourceOptions(
        replace_on_changes=["target"],
        delete_before_replace=True
    )
)

news_route = aws_native.apigatewayv2.Route("stockNewsRoute",
    api_id=http_api.id,
    route_key="GET /api/company-news",
    target=format_integration_target(lambda_integration.id),
    authorization_type="JWT", # Protect this route
    authorizer_id=jwt_authorizer.authorizer_id, # Link to the authorizer
    opts=pulumi.ResourceOptions(
        replace_on_changes=["target"],
        delete_before_replace=True
    )
)

portfolio_get_route = aws_native.apigatewayv2.Route("portfolioGetRoute",
    api_id=http_api.id,
    route_key="GET /api/portfolio", # New route for portfolio
    target=format_integration_target(lambda_integration.id),
    authorization_type="JWT", # Protect this route
    authorizer_id=jwt_authorizer.authorizer_id, # Link to the authorizer
    opts=pulumi.ResourceOptions(
        replace_on_changes=["target"],
        delete_before_replace=True
    )
)

portfolio_post_route = aws_native.apigatewayv2.Route("portfolioPostRoute",
    api_id=http_api.id,
    route_key="POST /api/portfolio", # POST method for portfolio
    target=format_integration_target(lambda_integration.id),
    authorization_type="JWT", # Protect this route
    authorizer_id=jwt_authorizer.authorizer_id, # Link to the authorizer
    opts=pulumi.ResourceOptions(
        replace_on_changes=["target"],
        delete_before_replace=True
    )
)

# API Gateway automatically creates a default stage named '$default' for HTTP APIs
# and the invoke URL is directly available.

# Grant API Gateway permission to invoke the Lambda function
lambda_permission = aws.lambda_.Permission("bro-ker-ApiLambdaPermission",
    action="lambda:InvokeFunction",
    function=stock_api_lambda.name,
    principal="apigateway.amazonaws.com",
    # Correctly construct the source ARN for an HTTP API
    # Pass the Output objects directly to pulumi.Output.all
    source_arn=pulumi.Output.all(
        http_api.id,                   # This is an Output[str]
        aws.get_caller_identity().account_id, # This is an Output[str]
        aws.get_region().name          # This is an Output[str]
    ).apply(lambda args:
        # args is now a list: [api_id, account_id, region_name]
        f"arn:aws:execute-api:{args[2]}:{args[1]}:{args[0]}/*/*"
    )
)


# --- Deployment ---
# A default stage `$default` is automatically created for HTTP APIs.
# The invoke URL will be outputted by Pulumi.

# --- Outputs ---
pulumi.export("lambda_function_name", stock_api_lambda.name)
pulumi.export("lambda_function_arn", stock_api_lambda.arn)
pulumi.export("lambda_role_arn", lambda_role.arn)
pulumi.export("api_gateway_id", http_api.id)
pulumi.export("dynamodb_table_name", dynamodb_table.name)
pulumi.export("api_gateway_invoke_url", http_api.api_endpoint) # This is the base URL for your API

## NEW ## --- Add Cognito outputs for your frontend app ---
pulumi.export("cognito_user_pool_id", user_pool.id)
pulumi.export("cognito_user_pool_client_id", user_pool_client.id)