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
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY_FOR_PULUMI")
if not FINNHUB_API_KEY:
    raise Exception("FINNHUB_API_KEY_FOR_PULUMI environment variable not set for Pulumi deployment.")

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
            "FINNHUB_API_KEY": FINNHUB_API_KEY,
            # Add any other environment variables your Lambda needs
        },
    ),
    tags={
        "Name": "stock-api-function",
        "Project": "Bro-Ker",
    })

# --- API Gateway (HTTP API v2) ---
# Create an HTTP API
http_api = aws_native.apigatewayv2.Api("stockHttpApi",
    name="StockBrokerHttpApi",
    protocol_type="HTTP",
    description="API for Bro-Ker stock information",
    target=stock_api_lambda.invoke_arn,  # Directly set the Lambda function ARN as the target
    # Corrected CORS Configuration using a dictionary:
    cors_configuration={ 
        "allowOrigins": ["https://bro-ker.com", "http://localhost:5500", "http://127.0.0.1:5500"],
        "allowMethods": ["GET", "OPTIONS"],
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

# Create Lambda integration for API Gateway
lambda_integration = aws_native.apigatewayv2.Integration("bro-ker-backend",
    api_id=http_api.id,
    integration_type="AWS_PROXY",
    integration_uri=stock_api_lambda.invoke_arn,
    payload_format_version="2.0"
)


pulumi.export("lambda_integration_id", lambda_integration.id)

# Create routes for the API
quotes_route = aws_native.apigatewayv2.Route("stockQuotesRoute",
    api_id=http_api.id,
    route_key="GET /api/stock-quotes",
    # Corrected target by splitting the composite ID
    target=lambda_integration.id.apply(
        lambda composite_id: f"integrations/{composite_id.split('|')[1]}" if isinstance(composite_id, str) and '|' in composite_id else "integrations/error-invalid-id-format"
    )
)

news_route = aws_native.apigatewayv2.Route("stockNewsRoute",
    api_id=http_api.id,
    route_key="GET /api/company-news",
    # Corrected target by splitting the composite ID
    target=lambda_integration.id.apply(
        lambda composite_id: f"integrations/{composite_id.split('|')[1]}" if isinstance(composite_id, str) and '|' in composite_id else "integrations/error-invalid-id-format"
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
pulumi.export("api_gateway_invoke_url", http_api.api_endpoint) # This is the base URL for your API