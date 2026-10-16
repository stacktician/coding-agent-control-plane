workspace "Coding agent control plane" "Issue-to-PR coding agent driven by Temporal, calling MCP servers through an MCP gateway under IdP-issued identity." {

    model {
        engineer = person "Engineer" "Labels issues for the agent, reviews its pull requests, watches the three consoles."

        github = softwareSystem "GitHub" "The target fork (GITHUB_FORK): issues, labels, pull requests, Git Data API." "External"
        anthropic = softwareSystem "Anthropic API" "Claude models, reached on the host's Claude subscription login." "External"
        langfuse = softwareSystem "Langfuse Cloud" "Agent observability: model turns, tool calls and test runs per issue session." "External"
        docker = softwareSystem "Docker Engine" "Host container runtime (Docker Desktop)." "Infrastructure"

        cacp = softwareSystem "Coding agent control plane" "Turns agent-ok issues into reviewed pull requests; every agent tool call crosses an authenticated MCP gateway." {

            worker = container "Temporal worker" "Runs PollWorkflow, IssueWorkflow and every activity on task queue coding-agent. Refuses to start if ANTHROPIC_API_KEY is set." "Python 3.12, Temporal Python SDK, Claude Agent SDK" "Host process" {
                poller = component "Ingress poller" "PollWorkflow + poll_fork, run by the Temporal Schedule poll-fork every 60 s: starts IssueWorkflow issue-<n> per open agent-ok issue (duplicate IDs rejected) and signals review labels found on agent PRs." "ingress/poller.py"
                issueWorkflow = component "IssueWorkflow" "One durable workflow per issue: claim, sandbox, agent, tests, PR, wait for the review signal, merge or close, outcome. Deterministic; side effects only in activities." "workflows/issue.py"
                issueActivities = component "Issue activities" "claim_issue, create_sandbox, run_tests, open_pull, remove_sandbox, apply_review, report_outcome." "workflows/activities.py"
                agentActivity = component "Agent activity" "run_agent: fetches a gateway token, launches the agent with only the gateway MCP server and the Task tool, heartbeats every 20 s." "agent/run.py"
                agentTrace = component "Agent trace recorder" "Turns the SDK message stream into Langfuse spans: a generation per model turn, a span per tool call, subagents nested." "agent/tracing.py"
                githubClient = component "GitHub client" "REST with the fork-scoped PAT. Publishes branches through the Git Data API, so no credential enters the sandbox." "github/client.py"
                identity = component "Identity" "Keycloak client-credentials token. Baseline: one shared identity." "identity.py"
                sandboxDriver = component "Sandbox driver" "Creates, execs into, reads, writes and removes sandbox containers through the Docker API. Shared with sandbox-mcp." "sandbox/containers.py"
                telemetry = component "Telemetry" "Replay-safe Jaeger provider for service spans; separate Langfuse provider for agent spans." "telemetry.py"
            }

            runtime = container "Claude Code runtime" "Agent loop launched per run by the Agent SDK. Built-in tools off except Task (test-writer subagent); no local settings, hooks or connectors." "Claude Code" "Host process"
            temporal = container "Temporal" "Durable workflow history, task queue coding-agent, Schedule poll-fork, Web UI." "temporalio/temporal 1.9.1, dev server, SQLite"
            gateway = container "agentgateway" "Single MCP endpoint: JWT check, sandbox_ and github_ prefix routing, GitHub credential injection." "agentgateway 1.5.0"
            githubMcp = container "github-mcp" "GitHub as MCP tools: repos, issues and pull_requests toolsets." "github-mcp-server 1.12.2"
            sandboxMcp = container "sandbox-mcp" "run_command, read_file, write_file, edit_file and list_files inside an issue's sandbox." "Python 3.12, FastMCP"
            sandbox = container "Issue sandbox" "One per issue, cacp-sandbox-issue-<n>: shallow clone of the fork, install, tests. Holds no credentials." "python:3.12-slim + git" "Ephemeral"
            keycloak = container "Keycloak" "Realm control-plane; client agent-worker gets tokens for audience agentgateway." "Keycloak 26.7.4"
            jaeger = container "Jaeger" "Service traces: workflows, activities, gateway, sandbox-mcp." "Jaeger 2.21.0"
        }

        # People
        engineer -> github "Labels issues agent-ok; labels agent PRs agent-approve or agent-reject"
        engineer -> temporal "Watches workflow stages" "Web UI, :8233"
        engineer -> jaeger "Inspects service traces" "Web UI, :16686"
        engineer -> langfuse "Inspects agent traces" "Web UI"

        # Container level, declared before the component level so these labels win
        worker -> temporal "Polls tasks; starts IssueWorkflows; signals review decisions" "gRPC, :7233"
        worker -> github "Issues, labels, comments; publishes branches; opens, merges and closes PRs" "REST, fine-grained PAT"
        worker -> keycloak "Fetches a client-credentials token" "OIDC, :8080"
        worker -> docker "Creates, execs into and removes sandboxes" "Docker API"
        worker -> runtime "Launches one agent run per issue" "Claude Agent SDK"
        worker -> jaeger "Exports workflow and activity spans" "OTLP/HTTP, :4318"
        worker -> langfuse "Exports agent and test spans" "OTLP/HTTP"
        runtime -> anthropic "Model turns" "HTTPS, Claude login"
        runtime -> gateway "Every tool call, with bearer token and traceparent" "MCP Streamable HTTP, :3000"
        gateway -> keycloak "Fetches signing keys to validate JWTs" "JWKS"
        gateway -> githubMcp "Routes github_* tools, injects the PAT" "MCP HTTP, :8082"
        gateway -> sandboxMcp "Routes sandbox_* tools" "MCP HTTP, :8083"
        gateway -> jaeger "Exports gateway spans" "OTLP/gRPC, :4317"
        githubMcp -> github "Reads issues and comments" "REST"
        sandboxMcp -> docker "Exec, read and write in the issue's sandbox" "docker.sock (root-equivalent)"
        sandboxMcp -> jaeger "Exports tool spans" "OTLP/HTTP, :4318"
        docker -> sandbox "Runs"
        sandbox -> github "Clones the fork, depth 50" "git over HTTPS, anonymous"

        # Component level
        poller -> githubClient "Lists agent-ok issues and agent PRs"
        poller -> temporal "Starts IssueWorkflow issue-<n>; signals review"
        issueWorkflow -> issueActivities "Executes, with retries and timeouts"
        issueWorkflow -> agentActivity "Executes, 60 min timeout, 2 min heartbeat"
        issueActivities -> githubClient "Claims, publishes, merges or closes, reports"
        issueActivities -> sandboxDriver "Creates, tests in and removes the sandbox"
        issueActivities -> telemetry "Records the test run"
        agentActivity -> identity "Gets the gateway token"
        agentActivity -> runtime "Runs the agent"
        agentActivity -> agentTrace "Feeds the message stream"
        agentTrace -> telemetry "Uses the Langfuse tracer"
        githubClient -> github "REST calls"
        identity -> keycloak "Client-credentials grant"
        sandboxDriver -> docker "Docker API calls"
        telemetry -> jaeger "Service spans"
        telemetry -> langfuse "Agent spans"

        deploymentEnvironment "Baseline" {
            deploymentNode "Developer workstation" "" "Windows 11" {
                deploymentNode "Host processes" "Run on the host so the agent uses the Claude login." "uv, Python 3.12" {
                    containerInstance worker
                    containerInstance runtime
                }
                deploymentNode "Docker Desktop" "" "Docker Engine" {
                    softwareSystemInstance docker
                    deploymentNode "Compose project coding-agent-control-plane" "Ports published on 127.0.0.1 only." "Docker Compose" {
                        containerInstance temporal
                        containerInstance keycloak
                        containerInstance gateway
                        containerInstance githubMcp
                        containerInstance sandboxMcp
                        containerInstance jaeger
                    }
                    deploymentNode "Issue sandboxes" "Created by the worker, one per issue." "Docker container" {
                        containerInstance sandbox
                    }
                }
            }
            deploymentNode "github.com" "" "SaaS" {
                softwareSystemInstance github
            }
            deploymentNode "Anthropic" "" "SaaS" {
                softwareSystemInstance anthropic
            }
            deploymentNode "Langfuse Cloud" "" "SaaS" {
                softwareSystemInstance langfuse
            }
        }
    }

    views {
        systemContext cacp "SystemContext" "Who and what the control plane talks to." {
            include *
            autoLayout lr
        }

        container cacp "AgentToolPath" "How the agent reaches models, tools and the sandbox, and where identity is checked." {
            include worker runtime gateway keycloak githubMcp sandboxMcp sandbox docker github anthropic
            autoLayout lr
        }

        container cacp "OrchestrationAndObservability" "Durable orchestration and the three consoles the engineer watches." {
            include engineer worker temporal jaeger langfuse gateway sandboxMcp
            exclude "gateway -> sandboxMcp"
            autoLayout lr
        }

        component worker "WorkerComponents" "Inside the Temporal worker." {
            include *
            autoLayout lr
        }

        dynamic cacp "IssueToPullRequest" "One issue, from the agent-ok label to a reviewed pull request." {
            worker -> github "Poll finds an open agent-ok issue"
            worker -> temporal "Starts IssueWorkflow issue-<n>"
            worker -> github "Claims it: agent-claimed label and comment"
            worker -> docker "Creates sandbox cacp-sandbox-issue-<n>"
            sandbox -> github "Clones the fork; installs"
            worker -> keycloak "Fetches the gateway token"
            worker -> runtime "Launches the agent run"
            runtime -> anthropic "Model turns"
            runtime -> gateway "Tool calls with the bearer token"
            gateway -> githubMcp "github_*: reads the issue and comments"
            githubMcp -> github "GitHub API with the injected PAT"
            gateway -> sandboxMcp "sandbox_*: reads, edits, runs tests"
            sandboxMcp -> docker "Exec and file writes in the sandbox"
            worker -> docker "Runs the full test suite; collects changes"
            worker -> github "Publishes agent/issue-<n> via Git Data API; opens the PR"
            worker -> docker "Removes the sandbox"
            engineer -> github "Labels the PR agent-approve or agent-reject"
            worker -> github "Next poll finds the review label"
            worker -> temporal "Signals the review to issue-<n>"
            worker -> github "Squash-merges or closes; comments the outcome; removes labels"
            properties {
                "plantuml.sequenceDiagram" "true"
            }
        }

        deployment cacp "Baseline" "Deployment" "Where each part runs in the baseline." {
            include *
            autoLayout tb
        }

        styles {
            element "Person" {
                shape Person
                background #08427b
                color #ffffff
            }
            element "Software System" {
                background #1168bd
                color #ffffff
            }
            element "Container" {
                background #438dd5
                color #ffffff
            }
            element "Component" {
                background #85bbf0
                color #000000
            }
            element "External" {
                background #8a8a8a
                color #ffffff
            }
            element "Infrastructure" {
                background #5a5a5a
                color #ffffff
            }
            element "Ephemeral" {
                border dashed
            }
            element "Boundary" {
                color #0b4884
            }
        }
    }
}
