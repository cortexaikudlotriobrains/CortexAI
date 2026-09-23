# Test Suite Documentation

## Overview

This test suite provides comprehensive testing for the OpenAI Project, including:
1. **Unified Response Contract Tests** - Ensures all provider clients adhere to the locked UnifiedResponse contract
2. **Model Utils Tests** - Tests for `ModelUtils` and `GeminiClient` classes (unit and integration tests)
3. **FastAPI Contract & Guardrails Tests** - Validates API contract, input validation, and safety guarantees
4. **Multi-Model Compare Mode Tests** - Tests for multi-provider comparison functionality
5. **Prompt Optimizer Tests** - Comprehensive testing of prompt optimization features
6. **Conversation Manager Tests** - Tests for multi-turn conversation support
7. **API Integration Tests** - Quick API endpoint testing
8. **Regression Tests** - Specific bug regression prevention
9. **Refactoring Tests** - Validates core orchestrator functionality

---

## Test Files Summary

| Test File | Purpose | Test Count | Key Features |
|-----------|---------|------------|--------------|
| `test_unified_response_contract.py` | Contract enforcement | 15 tests | Provider compliance, error normalization |
| `test_model_utils.py` | Model utilities | 21 tests | Unit + integration tests |
| `test_fastapi_contract_and_guardrails.py` | API contract validation | 8 tests | Input validation, safety guarantees |
| `test_baseline_safety_rails.py` | Provider safety rails | 20+ tests | Schema/provider/byok/orchestrator regression contracts |
| `test_provider_catalog.py` | Provider catalog contract | 6 tests | SSoT mapping and module wiring checks |
| `test_client_registry_and_schema_dynamic.py` | Registry + dynamic schema | 7 tests | Adapter registry factory behavior, catalog-driven schema acceptance |
| `test_dynamic_provider_discovery_e2e.py` | Discovery endpoint regression | 1 test | New provider appears in `/v1/providers`, `/v1/model-options`, and `/v1/models` automatically |
| `test_component_boundaries.py` | Deploy boundary regression | 5 tests | API-only mode, missing frontend handling, React default path, override precedence, and runtime health |
| `test_multi_compare_mode.py` | Multi-provider comparison | 15+ tests | Concurrent execution, error handling |
| `test_prompt_optimizer.py` | Prompt optimization | 20+ tests | Input validation, self-correction |
| `test_conversation.py` | Conversation management | 10+ tests | Multi-turn support, auto-trimming |
| `test_api.py` | API endpoint testing | 4 tests | Health, chat, compare endpoints |
| `test_compare_session_totals.py` | Regression test | 1 test | Session total calculation bug |
| `test_refactor.py` | Core functionality | 4 tests | Orchestrator, UserContext |

---

## Unified Response Contract Tests

### Purpose

The `test_unified_response_contract.py` test suite **locks and enforces** the UnifiedResponse contract across all AI provider clients (OpenAI, DeepSeek, Grok, Gemini). These tests ensure that:

- All providers return `UnifiedResponse` objects (never provider-specific responses)
- No exceptions bubble up from provider clients (errors returned as structured responses)
- Token usage, cost, latency, and request IDs are always present
- Error handling is consistent with normalized error codes
- Main application code never needs to access provider-specific fields

### Test Structure

The test suite contains **15 tests** organized into 4 test classes:

#### 1. TestUnifiedResponseContract (5 tests)
Tests the core UnifiedResponse data models:

- `test_unified_response_creation` - Validates all required fields can be created
- `test_unified_response_with_error` - Ensures error responses work correctly
- `test_token_usage_auto_total` - Verifies TokenUsage auto-calculates total_tokens
- `test_normalized_error_validates_code` - Checks error code validation (invalid → "unknown")
- `test_finish_reason_validation` - Validates finish_reason normalization

#### 2. TestProviderContractCompliance (4 tests)
Tests that all provider clients return UnifiedResponse:

- `test_openai_returns_unified_response` - OpenAI client returns UnifiedResponse on success
- `test_openai_handles_errors_gracefully` - OpenAI returns error UnifiedResponse (no exception)
- `test_deepseek_returns_unified_response` - DeepSeek client compliance
- `test_grok_returns_unified_response` - Grok client compliance
- `test_gemini_returns_unified_response` - Gemini client compliance (requires google-genai package)

#### 3. TestErrorHandlingContract (3 tests)
Tests that errors are normalized correctly:

- `test_timeout_error_normalized` - Timeout errors → code="timeout", retryable=True
- `test_auth_error_normalized` - Auth errors → code="auth", retryable=False
- `test_rate_limit_error_normalized` - Rate limit errors → code="rate_limit", retryable=True

#### 4. TestTokenTrackerIntegration (2 tests)
Tests that TokenTracker works with UnifiedResponse:

- `test_token_tracker_accepts_unified_response` - TokenTracker can accept UnifiedResponse directly
- `test_token_tracker_backward_compatibility` - TokenTracker still accepts dict format

---

## FastAPI Contract & Guardrails Tests

### Purpose

The `test_fastapi_contract_and_guardrails.py` test suite validates the *public API contract, guardrails, and safety guarantees* of the CortexAI FastAPI application. These tests operate WITHOUT calling real LLM providers, external APIs, or incurring token costs.

### Key Features

1. **API Health & Availability** - Confirms service reachability (`/health`)
2. **Authentication & Guardrails** - Ensures protected endpoints reject unauthorized requests
3. **Input Validation** - Enforces constraints on compare requests (min/max targets)
4. **Error Normalization** - Verifies consistent error handling across the API
5. **DTO Contract Stability** - Ensures CompareResponseDTO mapping compatibility
6. **Runtime Safety** - Guarantees no HTTP 500 errors from user input

### Test Classes

- **Authentication Tests** - API key validation, endpoint protection
- **Input Validation Tests** - Request structure validation, boundary conditions
- **Error Handling Tests** - Exception normalization, retryable vs non-retryable
- **DTO Mapping Tests** - Response contract stability
- **Safety Tests** - No 500 errors guarantee

---

## Baseline Safety Rails Tests

### Purpose

The `test_baseline_safety_rails.py` suite freezes current provider-related behavior before larger architectural refactors.

### Key Features

1. **Schema Contract Freeze** - Locks current provider/model validation in request schemas
2. **Chat Helper Contract Freeze** - Locks provider normalization and execution planning behavior
3. **BYOK Contract Freeze** - Locks provider allowlist and runtime key resolution behavior
4. **Orchestrator Guardrails** - Locks core non-smart routing and provider-type guardrails

### Why This Exists

These tests are a safety net for incremental architecture changes (provider registry, config-driven providers, deploy split) so behavior does not drift unintentionally.

---

## Multi-Model Compare Mode Tests

### Purpose

The `test_multi_compare_mode.py` test suite validates multi-provider comparison functionality through `MultiModelOrchestrator` and `MultiUnifiedResponse`.

### Key Features

1. **Concurrent Execution** - Tests that providers are called concurrently
2. **Order Preservation** - Ensures response order matches input order
3. **Error Handling** - Validates mixed success/failure scenarios
4. **Aggregation** - Tests token/cost aggregation across providers
5. **Timeout Handling** - Validates timeout behavior for slow providers

### Test Classes

#### TestMultiUnifiedResponse
- Empty responses handling
- Successful responses aggregation
- Error responses handling
- Immutability verification

#### TestMultiModelOrchestrator
- Single/multiple client scenarios
- Error handling and mixed results
- Concurrent execution validation
- Order preservation tests
- Timeout handling

#### TestIntegration
- Full comparison flow testing
- End-to-end validation

---

## Prompt Optimizer Tests

### Purpose

The `test_prompt_optimizer.py` test suite provides comprehensive testing of prompt optimization functionality with input validation, optimization flow, and self-correction mechanisms.

### Key Features

1. **Input Validation** - Validates prompt and settings input
2. **Output Validation** - Ensures proper JSON schema compliance
3. **Response Parsing** - Handles various OpenAI response formats
4. **Optimization Flow** - Tests complete optimization with mocked OpenAI
5. **Self-Correction** - Validates retry mechanisms for invalid responses
6. **Integration Tests** - Real API testing (optional)

### Test Classes

#### TestInputValidation
- Valid input scenarios (prompt only, with settings)
- Missing/empty prompt validation
- Type validation (string, dictionary)
- Settings validation

#### TestOutputValidation
- Valid output schemas (minimal, complete)
- Required field validation
- Type validation for optional fields

#### TestResponseParsing
- Plain JSON parsing
- Markdown code block parsing
- Invalid JSON error handling
- Missing field validation

#### TestOptimizationFlow
- Successful optimization with mocked OpenAI
- Settings integration
- API error handling

#### TestSelfCorrection
- Retry mechanisms for invalid JSON
- Multiple attempt validation

---

## Conversation Manager Tests

### Purpose

The `test_conversation.py` test suite validates multi-turn conversation support through `ConversationManager`.

### Key Features

1. **Basic Functionality** - Message addition, retrieval, role validation
2. **Auto-trimming** - Automatic message limit enforcement
3. **Message Operations** - pop_last_user, reset functionality
4. **System Prompt Handling** - System prompt preservation
5. **Format Validation** - Message structure validation
6. **Conversation Summary** - Summary generation

### Test Functions

- `test_conversation_manager()` - Core functionality testing
- `test_message_format()` - Message structure validation
- `test_conversation_summary()` - Summary functionality

---

## API Integration Tests

### Purpose

The `test_api.py` file provides quick API endpoint testing for development and validation.

### Test Functions

- `test_health()` - Health endpoint validation
- `test_chat()` - Chat endpoint testing
- `test_compare()` - Compare endpoint testing
- `test_auth()` - Authentication failure testing

### Usage

Run with server running on localhost:8000:
```bash
python tests/test_api.py
```

---

## Regression Tests

### Compare Session Totals Regression

The `test_compare_session_totals.py` file prevents regression of a specific bug where session totals in Compare Mode were incorrectly calculated using a single CostCalculator instance tied to MODEL_TYPE, causing wrong totals when responses came from different providers.

### Test Validation

- Session totals sum `estimated_cost` and `total_tokens` directly from UnifiedResponse objects
- Validates correct aggregation across multiple providers with different pricing
- Ensures session accumulation works correctly

---

## Refactoring Tests

### Purpose

The `test_refactor.py` file validates core orchestrator functionality after refactoring.

### Test Functions

- `test_orchestrator_initialization()` - CortexOrchestrator creation
- `test_user_context()` - UserContext manipulation
- `test_token_tracker_creation()` - TokenTracker creation
- `test_cost_calculator_creation()` - CostCalculator creation

---

## Running All Tests

### Quick Start

```bash
# Install dependencies
pip install pytest pytest-cov

# Run ALL tests
pytest tests/ -v

# Run specific test categories
pytest tests/test_unified_response_contract.py -v              # Contract tests
pytest tests/test_fastapi_contract_and_guardrails.py -v       # API contract tests
pytest tests/test_multi_compare_mode.py -v                    # Compare mode tests
pytest tests/test_prompt_optimizer.py -v                      # Prompt optimizer tests
pytest tests/test_conversation.py -v                          # Conversation tests
pytest tests/test_model_utils.py -v                          # Model utils tests

# Run with coverage
pytest tests/ --cov=models --cov=api --cov=utils --cov=orchestrator --cov-report=html

# Run unit tests only (no integration)
pytest tests/ -m "not integration"

# Run integration tests only
pytest tests/ -m integration
```

### Test Categories by Speed

#### Fast Tests (No API Keys Required)
- Unified Response Contract Tests
- FastAPI Contract & Guardrails Tests
- Multi-Model Compare Mode Tests
- Prompt Optimizer Unit Tests
- Conversation Manager Tests
- Regression Tests
- Refactoring Tests

#### Slow Tests (API Keys Required)
- Model Utils Integration Tests
- Prompt Optimizer Integration Tests
- API Integration Tests (requires running server)

---

## Test Coverage Goals

### Current Coverage Summary

**Total Tests**: 100+ tests across all test suites

**Coverage Breakdown**:
- **Contract Tests**: 15 tests (14 passing, 1 requires google-genai)
- **FastAPI Contract Tests**: 8 tests (all passing)
- **Multi-Compare Mode Tests**: 15+ tests (all passing)
- **Prompt Optimizer Tests**: 20+ tests (unit + integration)
- **Conversation Tests**: 10+ tests (all passing)
- **Model Utils Tests**: 21 tests (unit + integration)
- **API Tests**: 4 tests (requires server)
- **Regression Tests**: 1 test (bug prevention)
- **Refactoring Tests**: 4 tests (core validation)

### Coverage Targets
- Line Coverage: >90%
- Branch Coverage: >85%
- Function Coverage: 100%

### Contract Coverage
The unified response contract tests provide **100% coverage** of:
- All UnifiedResponse fields and properties
- All provider clients (OpenAI, DeepSeek, Grok, Gemini)
- All 6 error codes (timeout, auth, rate_limit, bad_request, provider_error, unknown)
- TokenTracker integration with UnifiedResponse

---

## Best Practices

### Running Tests Before Changes
```bash
# Before committing code
pytest tests/test_unified_response_contract.py -v  # Must pass
pytest tests/test_fastapi_contract_and_guardrails.py -v  # Must pass

# Before pushing to main
pytest tests/ -v  # Run all tests
```

### Test Development Guidelines
1. **Contract Tests** - Always add contract compliance for new providers
2. **Unit Tests** - Use mocks for fast, deterministic testing
3. **Integration Tests** - Mark with `@pytest.mark.integration`
4. **Error Testing** - Test both success and failure paths
5. **Documentation** - Add docstrings explaining test purpose

### CI/CD Recommendations
For CI/CD pipelines, run fast tests without integration tests:
```bash
pytest tests/test_unified_response_contract.py tests/test_fastapi_contract_and_guardrails.py tests/test_multi_compare_mode.py tests/test_prompt_optimizer.py tests/test_conversation.py -m "not integration" --cov=models --cov=api --cov=utils --cov=orchestrator --cov-report=xml
```

---

## Dependencies

Required packages:
```
# Core testing
pytest>=7.0.0
pytest-cov>=4.0.0

# For contract tests
openai>=1.0.0  # OpenAI, DeepSeek, Grok clients

# For model utils tests (optional)
google-generativeai>=0.3.0  # Gemini integration tests

# FastAPI testing
fastapi>=0.68.0
httpx>=0.20.0  # For TestClient

# Standard library (already included)
unittest.mock  # Mocking
```

Install all:
```bash
pip install -r requirements.txt
```

Minimum for contract tests only:
```bash
pip install pytest pytest-cov openai
```

---

## Summary

This comprehensive test suite provides multi-layer coverage:

### High Priority Tests (Must Pass)
- **Unified Response Contract Tests** (15 tests) - Enforces API contract
- **FastAPI Contract Tests** (8 tests) - Validates public API safety
- **Multi-Compare Mode Tests** (15+ tests) - Core comparison functionality

### Feature Tests (Important)
- **Prompt Optimizer Tests** (20+ tests) - Optimization features
- **Conversation Tests** (10+ tests) - Multi-turn support
- **Model Utils Tests** (21 tests) - Provider functionality

### Quality Assurance
- **Regression Tests** - Prevent known bugs
- **Refactoring Tests** - Validate core functionality
- **API Integration Tests** - End-to-end validation

### Key Benefits
1. **Type Safety** - Contract tests ensure consistent response types
2. **No Surprises** - Error handling is predictable across all providers
3. **Fast Feedback** - Most tests run in seconds without external dependencies
4. **Easy Refactoring** - Change internals without breaking contracts
5. **Documentation** - Tests serve as living documentation
6. **Regression Prevention** - Specific tests prevent known bugs from reoccurring

### Recommended Workflow
```bash
# Before committing code
pytest tests/test_unified_response_contract.py tests/test_fastapi_contract_and_guardrails.py -v

# Before pushing to main
pytest tests/ -v  # Run all tests

# When adding new provider
# 1. Implement provider client
# 2. Add contract test
# 3. Verify all contract tests pass
# 4. Add feature tests as needed
```

This test suite ensures the OpenAI Project remains stable, predictable, and safe as it evolves.
