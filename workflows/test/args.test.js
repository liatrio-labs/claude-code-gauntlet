import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ARGS_VERSION, normalizeArgs, normalizeArgsReport, validateArgs, stripNullOptionalsReport } from '../src/args.js';
import { nullToleranceGap, nullRespellGap, nullToleranceRejectedKeys, resolveReviewConfig } from '../src/args.js';
import { interactiveArgs, headlessArgs, priorReviewDetector, knobRows, validArgs, prIdentity } from './helpers/argsFixtures.js';

const REJECTION_ROWS = [
  { name: 'version', input: (a) => { a.argsVersion = 2; }, expected: [ "unsupported argsVersion 2 (expected 1)" ] },
  { name: 'mode', input: (a) => { a.mode = "bogus"; }, expected: [
    "invalid mode: bogus",
    "configEcho has unexpected key(s): model_tier, delivery_tier, pr_comment_cap, review_md",
  ] },
  { name: 'nonce', input: (a) => { a.nonce = "n 1"; }, expected: [
    "invalid nonce: must match /^[A-Za-z0-9._-]+$/ (AST-safe, non-splitting \u2014 interpolated into the verify " +
      "command argv per slice)",
  ] },
  { name: 'agent flags', input: (a) => { a.agentFlags = {}; }, expected: [
    "agentFlags is no longer accepted \u2014 the pipeline derives dimension flags itself from riskTable/scopeAnswer " +
      "(issue #24). Stamp riskTable (the Phase 2e per-file risk classification) and, only when the light/full gate " +
      "asked, scopeAnswer; do not stamp agentFlags.",
  ] },
  { name: 'policy provider',
    input: (a) => { a.policy.provider = "aws"; },
    expected: [ "invalid policy.provider: aws (expected one of firstParty, bedrock, vertex, foundry)" ] },
  { name: 'policy tier',
    input: (a) => { a.policy.tier = "standard"; }, expected: [ "invalid policy.tier: standard (expected optimized)" ] },
  { name: 'policy gateway',
    input: (a) => { a.policy.gateway = "true"; }, expected: [ "invalid policy.gateway: true (expected a boolean)" ] },
  { name: 'repoRoot type', input: (a) => { a.repoRoot = 42; }, expected: [ "repoRoot must be a non-empty string when present" ] },
  { name: 'repoRoot control', input: (a) => { a.repoRoot = "/p\n"; }, expected: [ "repoRoot must not contain a control character" ] },
  { name: 'outputDir type', input: (a) => { a.outputDir = 42; }, expected: [ "outputDir must be a non-empty string when present" ] },
  { name: 'outputDir control', input: (a) => { a.outputDir = "/p\n"; }, expected: [ "outputDir must not contain a control character" ] },
  { name: 'headShaShort type',
    input: (a) => { a.headShaShort = 42; }, expected: [ "headShaShort must be a non-empty string when present" ] },
  { name: 'headShaShort shell metacharacter', input: (a) => { a.headShaShort = 'abc;id'; }, expected: [
    "headShaShort must match /^[A-Za-z0-9._-]+$/ (AST-safe, non-splitting \u2014 interpolated into the verify command " +
      "argv)",
  ] },
  { name: 'headShaShort control',
    input: (a) => { a.headShaShort = "/p\n"; }, expected: [ "headShaShort must not contain a control character" ] },
  { name: 'diffPath type', input: (a) => { a.diffPath = 42; }, expected: [ "diffPath must be a non-empty string when present" ] },
  { name: 'diffPath control', input: (a) => { a.diffPath = "/p\n"; }, expected: [ "diffPath must not contain a control character" ] },
  { name: 'pluginRoot type', input: (a) => { a.pluginRoot = 42; }, expected: [ "pluginRoot must be a non-empty string when present" ] },
  { name: 'pluginRoot null', input: (a) => { a.pluginRoot = null; }, expected: [
    'pluginRoot must be a non-empty string when present',
  ] },
  { name: 'pluginRoot object', input: (a) => { a.pluginRoot = {}; }, expected: [
    'pluginRoot must be a non-empty string when present',
  ] },
  { name: 'pluginRoot control', input: (a) => { a.pluginRoot = "/p\n"; }, expected: [ "pluginRoot must not contain a control character" ] },
  { name: 'blank path', input: (a) => { a.diffPath = "   "; }, expected: [ "diffPath must be a non-empty string when present" ] },
  { name: 'empty path', input: (a) => { a.diffPath = ''; }, expected: [
    'diffPath must be a non-empty string when present',
  ] },
  { name: 'relative output',
    input: (a) => { a.outputDir = "relative"; }, expected: [ "outputDir must be an absolute path (POSIX /-prefix)" ] },
  { name: 'relative root',
    input: (a) => { a.repoRoot = "relative"; }, expected: [ "repoRoot must be an absolute path without .. segments or backslashes" ] },
  { name: 'root traversal',
    input: (a) => { a.repoRoot = "/r/../other"; }, expected: [ "repoRoot must be an absolute path without .. segments or backslashes" ] },
  { name: 'root backslash',
    input: (a) => { a.repoRoot = "/a\\b"; }, expected: [ "repoRoot must be an absolute path without .. segments or backslashes" ] },
  { name: 'unsafe SHA', input: (a) => { a.headShaShort = "bad sha"; }, expected: [
    "headShaShort must match /^[A-Za-z0-9._-]+$/ (AST-safe, non-splitting \u2014 interpolated into the verify command " +
      "argv)",
  ] },
  { name: 'plugin relative',
    input: (a) => { a.pluginRoot = "relative"; }, expected: [ "pluginRoot must be an absolute path (POSIX /-prefix)" ] },
  { name: 'plugin backtick', input: (a) => { a.pluginRoot = "/plugin`root"; }, expected: [ "pluginRoot must not contain a backtick" ] },
  { name: 'plugin traversal',
    input: (a) => { a.pluginRoot = "/plugin/../other"; }, expected: [ "pluginRoot must not contain a .. path segment" ] },
  { name: 'changed files type',
    input: (a) => { a.changedFiles = {}; }, expected: [ "changedFiles must be an array of repo-relative paths" ] },
  { name: 'changed files string', input: (a) => { a.changedFiles = 'a.js,b.js'; }, expected: [
    'changedFiles must be an array of repo-relative paths',
  ] },
  { name: 'changed lines type', input: (a) => { a.changedLines = "1"; }, expected: [ "changedLines must be a number" ] },
  { name: 'risk container', input: (a) => { a.riskTable = {}; }, expected: [ "riskTable must be an array of {path, risk} objects" ] },
  { name: 'risk entry', input: (a) => { a.riskTable = [null]; }, expected: [
    "riskTable[0] must be an object of the form {path, risk}",
    "riskTable is missing risk classification for changed file(s): a.js",
  ] },
  { name: 'risk string entry', input: (a) => { a.riskTable = ['a.js']; }, expected: [
    'riskTable[0] must be an object of the form {path, risk}',
    'riskTable is missing risk classification for changed file(s): a.js',
  ] },
  { name: 'risk path', input: (a) => { a.riskTable[0].path = ""; }, expected: [
    "riskTable[0].path must be a non-empty string",
    "riskTable is missing risk classification for changed file(s): a.js",
  ] },
  { name: 'risk enum',
    input: (a) => { a.riskTable[0].risk = "bogus"; }, expected: [ "riskTable[0].risk must be one of low, medium, high (got \"bogus\")" ] },
  { name: 'risk extra',
    input: (a) => { a.riskTable[0].note = 1; }, expected: [ "riskTable[0] has unexpected key(s): note (only path and risk are allowed)" ] },
  { name: 'risk missing path',
    input: (a) => { a.changedFiles.push("b.js"); }, expected: [ "riskTable is missing risk classification for changed file(s): b.js" ] },
  { name: 'risk surplus path',
    input: (a) => { a.riskTable.push({ path: "ghost.js", risk: "low" }); },
    expected: [ "riskTable classifies file(s) not present in changedFiles: ghost.js" ] },
  { name: 'scope enum',
    input: (a) => { a.scopeAnswer = "partial"; }, expected: [ "invalid scopeAnswer: partial (expected one of light, full)" ] },
  { name: 'medium light answer', input: (a) => { a.scopeAnswer = "light"; }, expected: [
    "scopeAnswer is \"light\" but the riskTable/changedLines are not light-eligible (not every file is low risk, or" +
      " changedLines >= 50) \u2014 the orchestrator answered a light/full question the gate never asked; omit " +
      "scopeAnswer",
  ] },
  { name: 'low 50 answer', input: (a) => { a.scopeAnswer = "light"; a.riskTable[0].risk = "low"; a.changedLines = 50; }, expected: [
    "scopeAnswer is \"light\" but the riskTable/changedLines are not light-eligible (not every file is low risk, or" +
      " changedLines >= 50) \u2014 the orchestrator answered a light/full question the gate never asked; omit " +
      "scopeAnswer",
  ] },
  { name: 'ineligible full answer', input: (a) => { a.scopeAnswer = "full"; }, expected: [
    "scopeAnswer is \"full\" but the riskTable/changedLines are not light-eligible (not every file is low risk, or " +
      "changedLines >= 50) \u2014 the orchestrator answered a light/full question the gate never asked; omit scopeAnswer",
  ] },
  { name: 'eligible missing answer', input: (a) => { a.riskTable[0].risk = "low"; }, expected: [
    "riskTable/changedLines are light-eligible but scopeAnswer is missing \u2014 the light/full gate must have asked, " +
      "and its answer must be stamped as scopeAnswer",
  ] },
  { name: 'contextLines domain',
    input: (a) => { a.contextLines = "1"; },
    expected: [ "contextLines must be a positive safe integer (the measured size of the shared context file) when present" ] },
  { name: 'contextLines null', input: (a) => { a.contextLines = null; }, expected: [
    'contextLines must be a positive safe integer (the measured size of the shared context file) when present',
  ] },
  { name: 'contextLines non-finite', input: (a) => { a.contextLines = NaN; }, expected: [
    'contextLines must be a positive safe integer (the measured size of the shared context file) when present',
  ] },
  { name: 'contextLines minimum',
    input: (a) => { a.contextLines = 0; },
    expected: [ "contextLines must be a positive safe integer (the measured size of the shared context file) when present" ] },
  { name: 'contextLines ceiling',
    input: (a) => { a.contextLines = 5000001; },
    expected: [ "contextLines is 5000001, above the 5000000 ceiling \u2014 that is not a review context, it is a mis-measurement" ] },
  { name: 'contextChars domain',
    input: (a) => { a.contextLines = 2028; a.contextChars = "1"; },
    expected: [ "contextChars must be a positive safe integer (the measured size of the shared context file) when present" ] },
  { name: 'contextChars unsafe integer', input: (a) => { a.contextLines = 1; a.contextChars = Number.MAX_SAFE_INTEGER + 2; },
    expected: [
      'contextChars must be a positive safe integer (the measured size of the shared context file) when present',
    ] },
  { name: 'contextChars ceiling', input: (a) => { a.contextLines = 2028; a.contextChars = 500000001; }, expected: [
    "contextChars is 500000001, above the 500000000 ceiling \u2014 that is not a review context, it is a " +
      "mis-measurement",
  ] },
  { name: 'chars alone',
    input: (a) => { a.contextChars = 94784; },
    expected: [ "contextChars requires contextLines \u2014 chars alone cannot size a line-offset read plan" ] },
  { name: 'review config shape',
    input: (a) => { a.reviewConfig = []; }, expected: [ "reviewConfig must be an object (the parseReviewMd output shape) when present" ] },
  { name: 'root confidence_threshold',
    input: (a) => { a.reviewConfig = { confidence_threshold: "90" }; },
    expected: [ "reviewConfig.confidence_threshold must be an integer from 0 through 100 when present" ] },
  { name: 'fractional threshold', input: (a) => { a.reviewConfig = { confidence_threshold: 1.5 }; }, expected: [
    'reviewConfig.confidence_threshold must be an integer from 0 through 100 when present',
  ] },
  { name: 'root security_min_confidence',
    input: (a) => { a.reviewConfig = { security_min_confidence: 101 }; },
    expected: [ "reviewConfig.security_min_confidence must be an integer from 0 through 100 when present" ] },
  { name: 'root severity_threshold', input: (a) => { a.reviewConfig = { severity_threshold: "urgent" }; }, expected: [
    "reviewConfig.severity_threshold must be one of critical|high|medium|low when present (pre-parsed REVIEW.md " +
      "enum)",
  ] },
  { name: 'undefined threshold',
    input: (a) => { a.reviewConfig = { confidence_threshold: undefined }; },
    expected: [ "reviewConfig.confidence_threshold must be an integer from 0 through 100 when present" ] },
  { name: 'root ignore shape',
    input: (a) => { a.reviewConfig = { ignore: {} }; }, expected: [ "reviewConfig.ignore must be an array of flat pattern strings" ] },
  { name: 'root ignore element', input: (a) => { a.reviewConfig = { ignore: [{}] }; }, expected: [
    "reviewConfig.ignore[0] must be a flat pattern string (got object) \u2014 parseReviewMd emits strings, never " +
      "objects",
  ] },
  { name: 'scopes container',
    input: (a) => { a.reviewConfig = { scopes: {} }; }, expected: [ "reviewConfig.scopes must be an array when present" ] },
  { name: 'scope null',
    input: (a) => { a.reviewConfig = { scopes: [null] }; }, expected: [ "reviewConfig.scopes[0] must be a plain object" ] },
  { name: 'scope array',
    input: (a) => { a.reviewConfig = { scopes: [[]] }; }, expected: [ "reviewConfig.scopes[0] must be a plain object" ] },
  { name: 'scope empty dir',
    input: (a) => { a.reviewConfig = { scopes: [{ dir: "" }] }; }, expected: [ "reviewConfig.scopes[0].dir must be a non-empty string" ] },
  { name: 'scope leading slash', input: (a) => { a.reviewConfig = { scopes: [{ dir: "/api" }] }; }, expected: [
    "reviewConfig.scopes[0].dir must not have a leading or trailing /",
    "reviewConfig.scopes[0].dir must not contain an empty path segment",
  ] },
  { name: 'scope backslash',
    input: (a) => { a.reviewConfig = { scopes: [{ dir: "api\\src" }] }; },
    expected: [ "reviewConfig.scopes[0].dir must use / separators, not backslashes" ] },
  { name: 'scope control',
    input: (a) => { a.reviewConfig = { scopes: [{ dir: "api\n" }] }; },
    expected: [ "reviewConfig.scopes[0].dir must not contain a control character" ] },
  { name: 'scope empty segment',
    input: (a) => { a.reviewConfig = { scopes: [{ dir: "api//src" }] }; },
    expected: [ "reviewConfig.scopes[0].dir must not contain an empty path segment" ] },
  { name: 'scope dot',
    input: (a) => { a.reviewConfig = { scopes: [{ dir: "api/./src" }] }; },
    expected: [ "reviewConfig.scopes[0].dir must not contain . or .. path segments" ] },
  { name: 'scope layer extra',
    input: (a) => { a.reviewConfig = { scopes: [{ dir: "api", extra: true }] }; },
    expected: [ "reviewConfig.scopes[0] has unexpected key(s): extra" ] },
  { name: 'scope confidence',
    input: (a) => { a.reviewConfig = { scopes: [{ dir: "api", confidence_threshold: "90" }] }; },
    expected: [ "reviewConfig.scopes[0].confidence_threshold must be an integer from 0 through 100 when present" ] },
  { name: 'scope security',
    input: (a) => { a.reviewConfig = { scopes: [{ dir: "api", security_min_confidence: -1 }] }; },
    expected: [ "reviewConfig.scopes[0].security_min_confidence must be an integer from 0 through 100 when present" ] },
  { name: 'scope severity', input: (a) => { a.reviewConfig = { scopes: [{ dir: "api", severity_threshold: "urgent" }] }; }, expected: [
    "reviewConfig.scopes[0].severity_threshold must be one of critical|high|medium|low when present (pre-parsed " +
      "REVIEW.md enum)",
  ] },
  { name: 'scope ignore', input: (a) => { a.reviewConfig = { scopes: [{ dir: "api", ignore: [42] }] }; }, expected: [
    "reviewConfig.scopes[0].ignore[0] must be a flat pattern string (got number) \u2014 parseReviewMd emits strings, " +
      "never objects",
  ] },
  { name: 'duplicate scopes',
    input: (a) => { a.reviewConfig = { scopes: [{ dir: "api" }, { dir: "api" }] }; },
    expected: [ "reviewConfig.scopes[1].dir duplicates another scope dir: api" ] },
  { name: 'exclusion shape',
    input: (a) => { a.exclusionPatterns = {}; }, expected: [ "exclusionPatterns must be an array of flat pattern strings" ] },
  { name: 'exclusion string', input: (a) => { a.exclusionPatterns = 'foo'; }, expected: [
    'exclusionPatterns must be an array of flat pattern strings',
  ] },
  { name: 'exclusion element',
    input: (a) => { a.exclusionPatterns = [42]; }, expected: [ "exclusionPatterns[0] must be a flat pattern string (got number)" ] },
  { name: 'review md shape',
    input: (a) => { a.reviewMd = {}; }, expected: [ "reviewMd must be an array of {path, text} objects when present" ] },
  { name: 'review md entry', input: (a) => { a.reviewMd = [null]; }, expected: [
    "reviewMd[0] must be an object of the form {path, text}",
    "configEcho.review_md does not match reviewMd",
  ] },
  { name: 'review md extra', input: (a) => {
    a.reviewMd = [{ path: "api/REVIEW.md", text: "", extra: 1 }]; a.configEcho.review_md.value = "present";
  }, expected: [ "reviewMd[0] has unexpected key(s): extra (only path and text are allowed)" ] },
  { name: 'review md path type', input: (a) => {
    a.reviewMd = [{ path: 1, text: "" }]; a.configEcho.review_md.value = "present";
  }, expected: [ "reviewMd[0].path must be a non-empty string" ] },
  { name: 'review md missing path', input: (a) => {
    a.reviewMd = [{ text: 'x' }]; a.configEcho.review_md.value = 'present';
  }, expected: ['reviewMd[0].path must be a non-empty string'] },
  { name: 'review md text type', input: (a) => {
    a.reviewMd = [{ path: "api/REVIEW.md", text: 1 }]; a.configEcho.review_md.value = "present";
  }, expected: [ "reviewMd[0].text must be a string" ] },
  { name: 'review md missing text', input: (a) => {
    a.reviewMd = [{ path: 'api/REVIEW.md' }]; a.configEcho.review_md.value = 'present';
  }, expected: ['reviewMd[0].text must be a string'] },
  { name: 'review path absolute', input: (a) => {
    a.reviewMd = [{ path: "/api/REVIEW.md", text: "" }]; a.configEcho.review_md.value = "present";
  }, expected: [
    "reviewMd[0].path must be repo-relative (must not start with /)",
    "reviewMd[0].path must not contain an empty path segment",
  ] },
  { name: 'review path trailing slash', input: (a) => {
    a.reviewMd = [{ path: "api/REVIEW.md/", text: "" }]; a.configEcho.review_md.value = "present";
  }, expected: [
    "reviewMd[0].path must not have a trailing /",
    "reviewMd[0].path must not contain an empty path segment",
    "reviewMd[0].path must end with exactly REVIEW.md",
  ] },
  { name: 'review path backslash', input: (a) => {
    a.reviewMd = [{ path: "api\\REVIEW.md", text: "" }]; a.configEcho.review_md.value = "present";
  }, expected: [
    "reviewMd[0].path must use / separators, not backslashes",
    "reviewMd[0].path must end with exactly REVIEW.md",
  ] },
  { name: 'review path control', input: (a) => {
    a.reviewMd = [{ path: "api\n/REVIEW.md", text: "" }]; a.configEcho.review_md.value = "present";
  }, expected: [ "reviewMd[0].path must not contain a control character" ] },
  { name: 'review path NUL', input: (a) => {
    a.reviewMd = [{ path: 'api\u0000/REVIEW.md', text: '' }]; a.configEcho.review_md.value = 'present';
  }, expected: ['reviewMd[0].path must not contain a control character'] },
  { name: 'review path empty segment', input: (a) => {
    a.reviewMd = [{ path: "api//REVIEW.md", text: "" }]; a.configEcho.review_md.value = "present";
  }, expected: [ "reviewMd[0].path must not contain an empty path segment" ] },
  { name: 'review path dot segment', input: (a) => {
    a.reviewMd = [{ path: "api/./REVIEW.md", text: "" }]; a.configEcho.review_md.value = "present";
  }, expected: [ "reviewMd[0].path must not contain . or .. path segments" ] },
  { name: 'review path wrong basename', input: (a) => {
    a.reviewMd = [{ path: "api/review.md", text: "" }]; a.configEcho.review_md.value = "present";
  }, expected: [ "reviewMd[0].path must end with exactly REVIEW.md" ] },
  { name: 'root without path', input: (a) => {
    a.reviewMd = [{ path: "REVIEW.md", text: "" }]; a.configEcho.review_md.value = "present";
  }, expected: [
    "reviewMd and reviewConfigPath must agree: reviewConfigPath must be non-null iff reviewMd contains an entry " +
      "with path exactly REVIEW.md",
  ] },
  { name: 'path without root', input: (a) => { a.reviewMd = []; a.reviewConfigPath = "/r/REVIEW.md"; }, expected: [
    "reviewMd and reviewConfigPath must agree: reviewConfigPath must be non-null iff reviewMd contains an entry " +
      "with path exactly REVIEW.md",
  ] },
  { name: 'raw parsed review', input: (a) => { a.reviewMd = []; a.reviewConfig = {}; }, expected: [
    "reviewMd and reviewConfig are both present \u2014 pass raw reviewMd or pre-parsed reviewConfig, not both (single " +
      "authority)",
  ] },
  { name: 'raw parsed exclusions', input: (a) => { a.exclusionsText = ""; a.exclusionPatterns = []; }, expected: [
    "exclusionsText and exclusionPatterns are both present \u2014 pass raw exclusionsText or pre-parsed " +
      "exclusionPatterns, not both (single authority)",
  ] },
  { name: 'exclusions text', input: (a) => { a.exclusionsText = 1; }, expected: [ "exclusionsText must be a string when present" ] },
  { name: 'delivery shape',
    input: (a) => { a.delivery = "all"; }, expected: [ "delivery must be an object of the form { tier } when present" ] },
  { name: 'delivery tier', input: (a) => { a.delivery = { tier: "suggestions_only" }; }, expected: [
    "invalid delivery.tier: suggestions_only (expected one of all, main_only)",
    "delivery.tier does not match configEcho.delivery_tier",
  ] },
  { name: 'identity shape', input: (a) => { a.delivery = { prIdentity: "org/repo#310" }; }, expected: [
    "delivery.prIdentity must be an object { owner, repo, pr_number, sha_full, platform, web_origin[, title] } " +
      "when present",
  ] },
  { name: 'identity owner',
    input: (a) => { a.delivery = { prIdentity: prIdentity() }; a.delivery.prIdentity.owner = ""; },
    expected: [ "delivery.prIdentity.owner must be a non-empty string" ] },
  { name: 'identity repo',
    input: (a) => { a.delivery = { prIdentity: prIdentity() }; a.delivery.prIdentity.repo = ""; },
    expected: [ "delivery.prIdentity.repo must be a non-empty string" ] },
  { name: 'identity pr_number', input: (a) => {
    a.delivery = { prIdentity: prIdentity() };
    a.delivery.prIdentity.pr_number = 0;
  }, expected: [ "delivery.prIdentity.pr_number must be a positive safe integer" ] },
  { name: 'identity pr_number negative', input: (a) => {
    a.delivery = { prIdentity: prIdentity({ pr_number: -1 }) };
  }, expected: ['delivery.prIdentity.pr_number must be a positive safe integer'] },
  { name: 'identity pr_number fraction', input: (a) => {
    a.delivery = { prIdentity: prIdentity({ pr_number: 1.5 }) };
  }, expected: ['delivery.prIdentity.pr_number must be a positive safe integer'] },
  { name: 'identity pr_number string', input: (a) => {
    a.delivery = { prIdentity: prIdentity({ pr_number: '7' }) };
  }, expected: ['delivery.prIdentity.pr_number must be a positive safe integer'] },
  { name: 'identity sha_full', input: (a) => {
    a.delivery = { prIdentity: prIdentity() };
    a.delivery.prIdentity.sha_full = "D".repeat(40);
  }, expected: [ "delivery.prIdentity.sha_full must be a 40-character lowercase hex commit id" ] },
  { name: 'identity platform', input: (a) => {
    a.delivery = { prIdentity: prIdentity() };
    a.delivery.prIdentity.platform = "bitbucket";
  }, expected: [ "delivery.prIdentity.platform must be one of github, gitlab" ] },
  { name: 'identity web_origin', input: (a) => {
    a.delivery = { prIdentity: prIdentity() };
    a.delivery.prIdentity.web_origin = "github.com";
  }, expected: [ "delivery.prIdentity.web_origin must be an http(s) origin: scheme, host and optional port only" ] },
  { name: 'identity title', input: (a) => {
    a.delivery = { prIdentity: prIdentity() };
    a.delivery.prIdentity.title = "   ";
  }, expected: [ "delivery.prIdentity.title must be a non-empty string when present" ] },
  { name: 'identity title type', input: (a) => {
    a.delivery = { prIdentity: prIdentity({ title: 5 }) };
  }, expected: ['delivery.prIdentity.title must be a non-empty string when present'] },
  { name: 'identity repo slash',
    input: (a) => { a.delivery = { prIdentity: prIdentity() }; a.delivery.prIdentity.repo = "a/b"; },
    expected: [ "delivery.prIdentity.repo must not contain \"/\"" ] },
  { name: 'identity required missing', input: (a) => {
    a.delivery = { prIdentity: prIdentity() };
    delete a.delivery.prIdentity.platform;
  }, expected: [ "delivery.prIdentity.platform must be one of github, gitlab" ] },
  { name: 'origin path', input: (a) => {
    a.delivery = { prIdentity: prIdentity() };
    a.delivery.prIdentity.web_origin = "https://github.com/x";
  }, expected: [ "delivery.prIdentity.web_origin must be an http(s) origin: scheme, host and optional port only" ] },
  { name: 'origin userinfo', input: (a) => {
    a.delivery = { prIdentity: prIdentity() };
    a.delivery.prIdentity.web_origin = "https://user@github.com";
  }, expected: [ "delivery.prIdentity.web_origin must be an http(s) origin: scheme, host and optional port only" ] },
  { name: 'origin zero port', input: (a) => {
    a.delivery = { prIdentity: prIdentity() };
    a.delivery.prIdentity.web_origin = "https://git.example:0";
  }, expected: [ "delivery.prIdentity.web_origin must be an http(s) origin: scheme, host and optional port only" ] },
  { name: 'origin high port', input: (a) => {
    a.delivery = { prIdentity: prIdentity() };
    a.delivery.prIdentity.web_origin = "https://git.example:65536";
  }, expected: [ "delivery.prIdentity.web_origin must be an http(s) origin: scheme, host and optional port only" ] },
  { name: 'origin long label', input: (a) => {
    a.delivery = { prIdentity: prIdentity() };
    a.delivery.prIdentity.web_origin = "https://" + "a".repeat(64) + ".example";
  }, expected: [ "delivery.prIdentity.web_origin must be an http(s) origin: scheme, host and optional port only" ] },
  { name: 'persist shape',
    input: (a) => { a.persist = []; },
    expected: [ "persist must be an object of the form { assembleScriptPath, returnPrimaries } when present" ] },
  { name: 'persist script', input: (a) => { a.persist = { assembleScriptPath: "" }; }, expected: [
    "persist.assembleScriptPath must be a non-empty string path to scripts/assemble_artifacts.py",
    "persist.assembleScriptPath must equal /plugin/scripts/assemble_artifacts.py",
  ] },
  { name: 'persist script number', input: (a) => { a.persist = { assembleScriptPath: 42 }; }, expected: [
    'persist.assembleScriptPath must be a non-empty string path to scripts/assemble_artifacts.py',
  ] },
  { name: 'persist script object', input: (a) => { a.persist = { assembleScriptPath: {} }; }, expected: [
    'persist.assembleScriptPath must be a non-empty string path to scripts/assemble_artifacts.py',
  ] },
  { name: 'persist script null', input: (a) => { a.persist = { assembleScriptPath: null }; }, expected: [
    'persist.assembleScriptPath must be a non-empty string path to scripts/assemble_artifacts.py',
  ] },
  { name: 'persist number', input: (a) => { a.persist = 3; }, expected: [
    'persist must be an object of the form { assembleScriptPath, returnPrimaries } when present',
  ] },
  { name: 'persist returns',
    input: (a) => { a.persist = { returnPrimaries: "false" }; }, expected: [ "persist.returnPrimaries must be a boolean when present" ] },
  { name: 'assemble identity',
    input: (a) => { a.persist = { assembleScriptPath: "/other/scripts/assemble_artifacts.py" }; },
    expected: [ "persist.assembleScriptPath must equal /plugin/scripts/assemble_artifacts.py" ] },
  { name: 'verify identity',
    input: (a) => { a.verify = { scriptPath: "/plugin/scripts/../verify_findings.py" }; },
    expected: [ "verify.scriptPath must equal /plugin/scripts/verify_findings.py" ] },
  { name: 'limits shape', input: (a) => { a.limits = []; }, expected: [ "limits must be an object when present" ] },
  { name: 'limits null', input: (a) => { a.limits = null; }, expected: [
    'limits must be an object when present',
  ] },
  { name: 'limits string', input: (a) => { a.limits = 'x'; }, expected: [
    'limits must be an object when present',
  ] },
  { name: 'limits unknown', input: (a) => { a.limits.verifySclieSize = 50; }, expected: [
    "unknown limits key: verifySclieSize (expected one of summarizeBucketSize, validateBatch, challengeCap, " +
      "verifySliceSize, deliveryCap, discoveryCap, maxLineSpan)",
  ] },
  { name: 'limits summarizeBucketSize',
    input: (a) => { a.limits.summarizeBucketSize = 0; },
    expected: [ "limits.summarizeBucketSize must be a positive safe integer when present" ] },
  { name: 'limits validateBatch',
    input: (a) => { a.limits.validateBatch = 0; }, expected: [ "limits.validateBatch must be a positive safe integer when present" ] },
  { name: 'limits verifySliceSize',
    input: (a) => { a.limits.verifySliceSize = 0; }, expected: [ "limits.verifySliceSize must be a positive safe integer when present" ] },
  { name: 'limits maxLineSpan',
    input: (a) => { a.limits.maxLineSpan = 0; }, expected: [ "limits.maxLineSpan must be a positive safe integer when present" ] },
  { name: 'limits challengeCap',
    input: (a) => { a.limits.challengeCap = -1; }, expected: [ "limits.challengeCap must be a non-negative safe integer when present" ] },
  { name: 'limits deliveryCap',
    input: (a) => { a.limits.deliveryCap = -1; },
    expected: [ "limits.deliveryCap must be null, absent, or a non-negative safe integer when present" ] },
  { name: 'limits discoveryCap',
    input: (a) => { a.limits.discoveryCap = 0; }, expected: [ "limits.discoveryCap must be a positive safe integer when present" ] },
  { name: 'limits integer domain',
    input: (a) => { a.limits.validateBatch = 1.5; }, expected: [ "limits.validateBatch must be a positive safe integer when present" ] },
  { name: 'limits string value', input: (a) => { a.limits.validateBatch = 'x'; }, expected: [
    'limits.validateBatch must be a positive safe integer when present',
  ] },
  { name: 'limits NaN value', input: (a) => { a.limits.validateBatch = NaN; }, expected: [
    'limits.validateBatch must be a positive safe integer when present',
  ] },
  { name: 'multi-error limits', input: (a) => {
    a.limits = { summarizeBucketSize: 0, validateBatch: -1, challengeCap: -1, verifySliceSize: 0,
    deliveryCap: -1, discoveryCap: 0, maxLineSpan: 0 };
  }, expected: [
    "limits.summarizeBucketSize must be a positive safe integer when present",
    "limits.validateBatch must be a positive safe integer when present",
    "limits.verifySliceSize must be a positive safe integer when present",
    "limits.maxLineSpan must be a positive safe integer when present",
    "limits.challengeCap must be a non-negative safe integer when present",
    "limits.deliveryCap must be null, absent, or a non-negative safe integer when present",
    "limits.discoveryCap must be a positive safe integer when present",
  ] },
  { name: 'scope shape',
    input: (a) => { a.reviewScope = []; }, expected: [ "reviewScope must be { requested, kind, since, commits, detector }" ] },
  { name: 'reviewScope null', input: (a) => { a.reviewScope = null; }, expected: [
    'reviewScope must be { requested, kind, since, commits, detector }',
  ] },
  { name: 'scope string', input: (a) => { a.reviewScope = 'scope'; }, expected: [
    'reviewScope must be { requested, kind, since, commits, detector }',
  ] },
  { name: 'reviewScope extra',
    input: (a) => { a.reviewScope.reason = "old reason"; }, expected: [ "reviewScope has unexpected key(s): reason" ] },
  { name: 'scope requested',
    input: (a) => { a.reviewScope.requested = "skip"; }, expected: [ "reviewScope.requested must be full or incremental" ] },
  { name: 'scope kind', input: (a) => { a.reviewScope.kind = "bogus"; }, expected: [ "reviewScope.kind must be full or incremental" ] },
  { name: 'full since', input: (a) => { a.reviewScope.since = "abc"; }, expected: [ "reviewScope.since must be null for full scope" ] },
  { name: 'full commits', input: (a) => { a.reviewScope.commits = 1; }, expected: [ "reviewScope.commits must be null for full scope" ] },
  { name: 'incremental requested', input: (a) => {
    a.reviewScope = { requested: "incremental", kind: "incremental", since: "abc-1._x", commits: null,
    detector: priorReviewDetector() }; a.reviewScope.requested = "full";
  }, expected: [ "reviewScope.kind incremental requires requested incremental" ] },
  { name: 'incremental since', input: (a) => {
    a.reviewScope = { requested: "incremental", kind: "incremental", since: "abc-1._x", commits: null,
    detector: priorReviewDetector() }; a.reviewScope.since = "bad sha!";
  }, expected: [ "reviewScope.since must be a NONCE_RE-safe string for incremental scope" ] },
  { name: 'incremental commits', input: (a) => {
    a.reviewScope = { requested: "incremental", kind: "incremental", since: "abc-1._x", commits: null,
    detector: priorReviewDetector() }; a.reviewScope.commits = -1;
  }, expected: [ "reviewScope.commits must be a non-negative safe integer or null" ] },
  { name: 'incremental unsafe', input: (a) => {
    a.reviewScope = { requested: "incremental", kind: "incremental", since: "abc-1._x", commits: null,
    detector: priorReviewDetector() }; a.reviewScope.detector.incremental_safe = false;
  }, expected: [ "reviewScope.kind incremental requires detector.incremental_safe true" ] },
  { name: 'incremental null detector', input: (a) => {
    a.reviewScope = { requested: "incremental", kind: "incremental", since: "abc-1._x", commits: null,
    detector: priorReviewDetector() }; a.reviewScope.detector = null;
  }, expected: [ "reviewScope.kind incremental requires detector.incremental_safe true" ] },
  { name: 'full fallback',
    input: (a) => { a.reviewScope.requested = "incremental"; },
    expected: [ "reviewScope full fallback requires detector facts when incremental was requested" ] },
  { name: 'detector shape',
    input: (a) => { a.reviewScope.detector = []; },
    expected: [ "reviewScope.detector must be null or an object copied from detect_prior_review.py" ] },
  { name: 'detector scalar', input: (a) => { a.reviewScope.detector = 1; }, expected: [
    'reviewScope.detector must be null or an object copied from detect_prior_review.py',
  ] },
  { name: 'detector extra',
    input: (a) => { a.reviewScope.detector = priorReviewDetector({ extra: true }); },
    expected: [ "reviewScope.detector has unexpected key(s): extra" ] },
  { name: 'detector missing', input: (a) => {
    a.reviewScope.detector = priorReviewDetector(); delete a.reviewScope.detector.error;
  }, expected: [
    "reviewScope.detector is missing key(s): error",
    "reviewScope.detector.error must be a string or null",
  ] },
  { name: 'detector previously_reviewed',
    input: (a) => { a.reviewScope.detector = priorReviewDetector({ previously_reviewed: "no" }); },
    expected: [ "reviewScope.detector.previously_reviewed must be a boolean" ] },
  { name: 'detector sha_resolvable',
    input: (a) => { a.reviewScope.detector = priorReviewDetector({ sha_resolvable: "no" }); },
    expected: [ "reviewScope.detector.sha_resolvable must be a boolean" ] },
  { name: 'detector head_advanced',
    input: (a) => { a.reviewScope.detector = priorReviewDetector({ head_advanced: "no" }); },
    expected: [ "reviewScope.detector.head_advanced must be a boolean" ] },
  { name: 'detector sha_is_ancestor',
    input: (a) => { a.reviewScope.detector = priorReviewDetector({ sha_is_ancestor: "no" }); },
    expected: [ "reviewScope.detector.sha_is_ancestor must be a boolean" ] },
  { name: 'detector incremental_safe',
    input: (a) => { a.reviewScope.detector = priorReviewDetector({ incremental_safe: "no" }); },
    expected: [ "reviewScope.detector.incremental_safe must be a boolean" ] },
  { name: 'detector error type',
    input: (a) => { a.reviewScope.detector = priorReviewDetector({ error: 2 }); },
    expected: [ "reviewScope.detector.error must be a string or null" ] },
  { name: 'detector error control',
    input: (a) => { a.reviewScope.detector = priorReviewDetector({ error: "line\nbreak" }); },
    expected: [ "reviewScope.detector.error must be single-line without control characters" ] },
  { name: 'headless cap absent', base: headlessArgs,
    input: (a) => { delete a.limits.deliveryCap; },
    expected: [ "headless limits.deliveryCap must be a number matching configEcho.pr_comment_cap" ] },
  { name: 'headless cap null', base: headlessArgs,
    input: (a) => { a.limits.deliveryCap = null; },
    expected: [ "headless limits.deliveryCap must be a number matching configEcho.pr_comment_cap" ] },
  { name: 'headless cap string', base: headlessArgs,
    input: (a) => { a.limits.deliveryCap = "1"; },
    expected: [ "limits.deliveryCap must be null, absent, or a non-negative safe integer when present" ] },
  { name: 'interactive cap null mismatch',
    input: (a) => { a.limits.deliveryCap = null; a.configEcho.pr_comment_cap.value = "5"; },
    expected: [ "configEcho.pr_comment_cap must be null when limits.deliveryCap is absent or null" ] },
  { name: 'interactive cap absent mismatch',
    input: (a) => { a.configEcho.pr_comment_cap.value = "5"; },
    expected: [ "configEcho.pr_comment_cap must be null when limits.deliveryCap is absent or null" ] },
  { name: 'tier effective all',
    input: (a) => { a.configEcho.delivery_tier.value = "main_only"; },
    expected: [ "configEcho.delivery_tier does not match delivery.tier" ] },
  { name: 'tier malformed receipt', base: headlessArgs, input: (a) => {
    a.delivery.tier = "main_only"; a.configEcho.delivery_tier.source = "fixed";
  }, expected: [ "configEcho.delivery_tier.source is invalid for headless" ] },
  { name: 'cap malformed receipt', base: headlessArgs, input: (a) => {
    a.limits.deliveryCap = 1; a.configEcho.pr_comment_cap.value = "007";
  }, expected: [ "configEcho.pr_comment_cap.value is invalid for headless" ] },
  { name: 'ineligible light receipt', base: headlessArgs, input: (a) => { a.configEcho.trivial_scope.value = "light"; }, expected: [
    "configEcho.trivial_scope is \"light\" but the riskTable/changedLines are not light-eligible (not every file is" +
      " low risk, or changedLines >= 50) \u2014 the orchestrator answered a light/full question the gate never asked",
  ], normalize: true, check: (out) => { assert.equal(out.scopeAnswer, undefined); } },
  { name: 'two scope refusals', base: headlessArgs, input: (a) => {
    a.scopeAnswer = "full"; a.configEcho.trivial_scope.value = "light";
  }, expected: [
    "scopeAnswer is \"full\" but the riskTable/changedLines are not light-eligible (not every file is low risk, or " +
      "changedLines >= 50) \u2014 the orchestrator answered a light/full question the gate never asked; omit scopeAnswer",
    "configEcho.trivial_scope is \"light\" but the riskTable/changedLines are not light-eligible (not every file is" +
      " low risk, or changedLines >= 50) \u2014 the orchestrator answered a light/full question the gate never asked",
  ] },
  { name: 'PR comments identity', base: headlessArgs,
    input: (a) => { a.configEcho.delivery.value = "pr_comments"; },
    expected: [ "delivery.prIdentity is required when configEcho.delivery contains pr_comments" ] },
  { name: 'headless cap lockstep', base: headlessArgs,
    input: (a) => { a.limits.deliveryCap = 7; }, expected: [ "limits.deliveryCap does not match configEcho.pr_comment_cap" ] },
  { name: 'headless tier lockstep', base: headlessArgs,
    input: (a) => { a.delivery.tier = "main_only"; }, expected: [ "delivery.tier does not match configEcho.delivery_tier" ] },
  { name: 'headless requested lockstep', base: headlessArgs,
    input: (a) => { a.reviewScope.requested = "incremental"; },
    expected: [ "reviewScope.requested does not match configEcho.reviewed_policy" ] },
  { name: 'malformed cap keeps later lockstep', base: headlessArgs, input: (a) => {
    a.limits.deliveryCap = 1; a.configEcho.pr_comment_cap.value = '007';
    a.reviewScope.requested = 'incremental';
  }, expected: [
    'configEcho.pr_comment_cap.value is invalid for headless',
    'reviewScope.requested does not match configEcho.reviewed_policy',
  ] },
  { name: 'headless answer lockstep', base: headlessArgs, input: (a) => {
    a.riskTable[0].risk = "low"; a.scopeAnswer = "full"; a.configEcho.trivial_scope.value = "light";
  }, expected: [ "scopeAnswer does not match configEcho.trivial_scope" ] },
  { name: 'interactive cap lockstep',
    input: (a) => { a.limits.deliveryCap = 1; }, expected: [ "limits.deliveryCap does not match configEcho.pr_comment_cap" ] },
  { name: 'derived source lockstep',
    input: (a) => { a.reviewMd = []; a.configEcho.review_md.value = "present"; },
    expected: [ "configEcho.review_md does not match reviewMd" ] },
  { name: 'conflicting requested normalization', base: headlessArgs,
    input: (a) => { a.configEcho.reviewed_policy.value = "incremental"; }, expected: [
    "reviewScope.requested does not match configEcho.reviewed_policy",
  ], normalize: true, check: (out) => { assert.equal(out.reviewScope.requested, "full"); } },
  { name: 'required limits remains absent', base: headlessArgs,
    input: (a) => { delete a.limits; },
    expected: [ "missing required field: limits" ], normalize: true, check: (out) => { assert.equal("limits" in out, false); } },
  { name: 'required scope remains absent', base: headlessArgs, input: (a) => { delete a.reviewScope; }, expected: [
    "missing required field: reviewScope",
  ], normalize: true, check: (out) => { assert.equal("reviewScope" in out, false); } },
  { name: 'local requested remains absent', base: headlessArgs, input: (a) => {
    delete a.reviewScope.requested; a.reviewScope.detector = null;
  }, expected: [
    "reviewScope.requested must be full or incremental",
  ], normalize: true, check: (out) => { assert.equal(out.reviewScope.requested, undefined); } },
  { name: 'malformed reviewed requested remains absent', base: headlessArgs, input: (a) => {
    delete a.reviewScope.requested; a.configEcho.reviewed_policy.value = "bogus";
  }, expected: [
    "configEcho.reviewed_policy.value is invalid for headless",
    "reviewScope.requested must be full or incremental",
  ], normalize: true, check: (out) => { assert.equal(out.reviewScope.requested, undefined); } },
  { name: 'missing config echo', input: (a) => { delete a.configEcho; }, expected: [ "missing required field: configEcho" ] },
  { name: 'null config echo',
    input: (a) => { a.configEcho = null; }, expected: [ "configEcho must be an object keyed by methodology knob" ] },
  { name: 'null model receipt stays null', input: (a) => { a.configEcho.model_tier.value = null; },
    expected: ['configEcho.model_tier.value must be a string'], normalize: true,
    check: (out) => { assert.equal(out.configEcho.model_tier.value, null); } },
  { name: 'malformed config remains', input: (a) => { a.reviewConfig = 42; }, expected: [
    "reviewConfig must be an object (the parseReviewMd output shape) when present",
  ], normalize: true, check: (out) => { assert.equal(out.reviewConfig, 42); } },
  { name: 'malformed persist remains', input: (a) => { a.persist = "bad"; }, expected: [
    "persist must be an object of the form { assembleScriptPath, returnPrimaries } when present",
  ], normalize: true, check: (out) => { assert.equal(out.persist, "bad"); } },
 ];
for (const { name, input, expected, base = interactiveArgs, normalize, check } of REJECTION_ROWS) {
  test(`validate: ${name}`, () => {
    const args = base();
    input(args);
    const out = normalize ? normalizeArgs(args) : args;
    assert.deepEqual(validateArgs(out), { ok: false, errors: expected });
    if (check) check(out);
  });
}

const NON_OBJECT_ROWS = [
  { name: 'undefined', input: undefined, expected: { ok: false, errors: ['args is not an object'] } },
  { name: 'string', input: 'waist', expected: { ok: false, errors: ['args is not an object'] } },
];
for (const { name, input, expected } of NON_OBJECT_ROWS) {
  test(`validate scalar: ${name}`, () => assert.deepEqual(validateArgs(input), expected));
}

const ACCEPTANCE_ROWS = [
  { name: 'base', input: () => interactiveArgs(), expected: { ok: true, errors: [] } },
  { name: 'local scope ignores policy lockstep', input: () => headlessArgs('incremental', { detector: null }),
    expected: { ok: true, errors: [] } },
  { name: 'headless', input: () => headlessArgs(), expected: { ok: true, errors: [] } },
  { name: 'wired builder', input: () => validArgs(), expected: { ok: true, errors: [] } },
  { name: 'optional domains', input: () => interactiveArgs({
    nonce: 'n-1.0_ab', policy: { tier: 'optimized', provider: null, gateway: null },
    contextLines: 5000000, contextChars: 500000000,
    reviewConfig: { confidence_threshold: 0, security_min_confidence: 100, severity_threshold: 'low', ignore: ['a.js'] },
    exclusionPatterns: ['*.js'], delivery: {}, persist: { returnPrimaries: false },
    limits: { challengeCap: 0, deliveryCap: null, discoveryCap: 1 },
  }), expected: { ok: true, errors: [] } },
  { name: 'light 49', input: () => interactiveArgs({
    riskTable: [{ path: 'a.js', risk: 'low' }], changedLines: 49, scopeAnswer: 'light',
  }), expected: { ok: true, errors: [] } },
  { name: 'eligible full', input: () => interactiveArgs({
    riskTable: [{ path: 'a.js', risk: 'low' }], scopeAnswer: 'full',
  }), expected: { ok: true, errors: [] } },
  { name: 'incremental', input: () => interactiveArgs({
    reviewScope: { requested: 'incremental', kind: 'incremental', since: 'abc-1._x', commits: 0, detector: priorReviewDetector() },
  }), expected: { ok: true, errors: [] } },
  { name: 'scripts at root', input: () => interactiveArgs({
    pluginRoot: '/', persist: { assembleScriptPath: '/scripts/assemble_artifacts.py' },
    verify: { scriptPath: '/scripts/verify_findings.py' },
  }), expected: { ok: true, errors: [] } },
  { name: 'scripts trailing root', input: () => interactiveArgs({
    pluginRoot: '/plugin/', persist: { assembleScriptPath: '/plugin/scripts/assemble_artifacts.py', returnPrimaries: true },
    verify: { scriptPath: '/plugin/scripts/verify_findings.py' },
  }), expected: { ok: true, errors: [] } },
  { name: 'IPv6 identity', input: () => interactiveArgs({ delivery: { prIdentity: {
    owner: 'o', repo: 'r', pr_number: 310, sha_full: 'dddddddddddddddddddddddddddddddddddddddd',
    platform: 'gitlab', web_origin: 'http://[::1]:8443', title: 'A useful title',
  } } }), expected: { ok: true, errors: [] } },
];
for (const { name, input, expected } of ACCEPTANCE_ROWS) {
  test(`accept: ${name}`, () => assert.deepEqual(validateArgs(input()), expected));
}

const REQUIRED_ROWS = [
  { name: 'ordered inventory', input: { argsVersion: 1 }, expected: { ok: false, errors: [
    'missing required field: mode', 'missing required field: repoRoot', 'missing required field: outputDir',
    'missing required field: headShaShort', 'missing required field: nonce', 'missing required field: generatedAt',
    'missing required field: diffPath', 'missing required field: changedFiles', 'missing required field: changedLines',
    'missing required field: riskTable', 'missing required field: policy', 'missing required field: limits',
    'missing required field: configEcho', 'missing required field: pluginRoot', 'missing required field: reviewScope',
  ] } },
  { name: 'version constant', input: ARGS_VERSION, expected: 1 },
];
for (const { name, input, expected } of REQUIRED_ROWS) {
  test(`required: ${name}`, () => assert.deepEqual(name === 'version constant' ? input : validateArgs(input), expected));
}

const STRIP_ROWS = [
  { name: 'scalar', input: null, expected: { args: null, dropped: [], respelled: [] } },
  { name: 'array', input: ['not', 'an', 'object'], expected: {
    args: ['not', 'an', 'object'], dropped: [], respelled: [],
  } },
  { name: 'string', input: 'not an object', expected: {
    args: 'not an object', dropped: [], respelled: [],
  } },
  { name: 'optional inventory', input: {
    reviewConfig: null, exclusionPatterns: null, reviewMd: null, exclusionsText: null,
    delivery: null, checkpoints: null, persist: null, scopeAnswer: null,
  }, expected: { args: {}, dropped: [
    'reviewConfig', 'exclusionPatterns', 'reviewMd', 'exclusionsText', 'delivery', 'checkpoints', 'persist', 'scopeAnswer',
  ], respelled: [] } },
  { name: 'nested inventory', input: { delivery: { tier: null, prIdentity: null } },
    expected: { args: { delivery: {} }, dropped: ['delivery.prIdentity', 'delivery.tier'], respelled: [] } },
  { name: 'title', input: { delivery: { prIdentity: { owner: 'o', title: null } } },
    expected: { args: { delivery: { prIdentity: { owner: 'o' } } }, dropped: ['delivery.prIdentity.title'], respelled: [] } },
  { name: 'meaningful nulls', input: { policy: { subagentModel: null }, limits: { deliveryCap: null, challengeCap: 0 } },
    expected: { args: { policy: { subagentModel: null }, limits: {
      deliveryCap: null, challengeCap: 0, summarizeBucketSize: 20, validateBatch: 25, verifySliceSize: 25, maxLineSpan: 100,
    } }, dropped: [], respelled: [] } },
  { name: 'partial limits', input: { limits: { summarizeBucketSize: 7, deliveryCap: 3 } },
    expected: { args: { limits: {
      summarizeBucketSize: 7, deliveryCap: 3, validateBatch: 25, challengeCap: 40, verifySliceSize: 25, maxLineSpan: 100,
    } }, dropped: [], respelled: [] } },
  { name: 'malformed limits', input: { limits: 'nope' }, expected: { args: { limits: 'nope' }, dropped: [], respelled: [] } },
  { name: 'interactive null receipt', input: {
    mode: 'interactive', configEcho: { pr_comment_cap: { value: null, source: 'default' } },
  }, expected: { args: { mode: 'interactive', configEcho: { pr_comment_cap: { value: 'null', source: 'default' } } },
    dropped: [], respelled: ['configEcho.pr_comment_cap.value'] } },
  { name: 'headless null receipt', input: { mode: 'headless', configEcho: { pr_comment_cap: { value: null } } },
    expected: { args: { mode: 'headless', configEcho: { pr_comment_cap: { value: null } } }, dropped: [], respelled: [] } },
  { name: 'absent receipt value', input: { mode: 'interactive', configEcho: { pr_comment_cap: { source: 'default' } } },
    expected: { args: { mode: 'interactive', configEcho: { pr_comment_cap: { source: 'default' } } }, dropped: [], respelled: [] } },
  { name: 'clean', input: { reviewConfig: {}, delivery: { tier: 'all' } },
    expected: { args: { reviewConfig: {}, delivery: { tier: 'all' } }, dropped: [], respelled: [] } },
];
for (const { name, input, expected } of STRIP_ROWS) {
  test(`strip: ${name}`, () => {
    const before = structuredClone(input);
    assert.deepEqual(stripNullOptionalsReport(input), expected);
    assert.deepEqual(input, before);
  });
}

const NORMALIZE_ROWS = [
  { name: 'undefined', input: () => undefined, expected: { args: undefined, dropped: [], respelled: [], derivedPaths: [] } },
  { name: 'object null', input: () => ({ persist: null }),
    expected: { args: {}, dropped: ['persist'], respelled: [], derivedPaths: [] } },
  { name: 'JSON null', input: () => '{"reviewConfig":null}',
    expected: { args: {}, dropped: ['reviewConfig'], respelled: [], derivedPaths: [] } },
  { name: 'null cap derivation', input: () => ({
    mode: 'interactive', limits: {}, configEcho: { pr_comment_cap: { value: 'null', source: 'default' } },
  }), expected: { args: {
    mode: 'interactive', limits: {
      summarizeBucketSize: 20, validateBatch: 25, challengeCap: 40, verifySliceSize: 25, maxLineSpan: 100, deliveryCap: null,
    }, configEcho: {
      pr_comment_cap: { value: 'null', source: 'default' }, review_md: { value: 'absent', source: 'discovery' },
    },
  }, dropped: [], respelled: [], derivedPaths: ['limits.deliveryCap'] } },
  { name: 'interactive waist', input: () => ({
    mode: 'interactive', limits: {}, configEcho: {
      pr_comment_cap: { value: '7', source: 'env' }, delivery_tier: { value: 'main_only', source: 'env' },
    },
  }), expected: { args: {
    mode: 'interactive', limits: {
      summarizeBucketSize: 20, validateBatch: 25, challengeCap: 40, verifySliceSize: 25, maxLineSpan: 100, deliveryCap: 7,
    }, configEcho: {
      pr_comment_cap: { value: '7', source: 'env' }, delivery_tier: { value: 'main_only', source: 'env' },
      review_md: { value: 'absent', source: 'discovery' },
    }, delivery: { tier: 'main_only' },
  }, dropped: [], respelled: [], derivedPaths: ['limits.deliveryCap', 'delivery.tier'] } },
];
for (const { name, input, expected } of NORMALIZE_ROWS) {
  test(`normalize: ${name}`, () => {
    const raw = input();
    const before = structuredClone(raw);
    assert.deepEqual(normalizeArgsReport(raw), expected);
    assert.deepEqual(normalizeArgs(raw), expected.args);
    assert.deepEqual(raw, before);
  });
}

const PROVENANCE_ROWS = [
  { name: 'child', input: { reviewMd: [{ path: 'api/REVIEW.md', text: 'subtree' }], reviewConfigPath: null },
    expected: { value: 'present', source: 'discovery' } },
  { name: 'empty raw overrides legacy', input: { reviewMd: [], reviewConfigPath: '/r/REVIEW.md' },
    expected: { value: 'absent', source: 'discovery' } },
  { name: 'legacy present', input: { reviewConfigPath: '/r/REVIEW.md' }, expected: { value: 'present', source: 'discovery' } },
  { name: 'legacy absent', input: { reviewConfigPath: null }, expected: { value: 'absent', source: 'discovery' } },
];
for (const { name, input, expected } of PROVENANCE_ROWS) {
  test(`provenance: ${name}`, () => {
    const args = interactiveArgs(input);
    delete args.configEcho.review_md;
    assert.deepEqual(normalizeArgs(args).configEcho.review_md, expected);
  });
}

const POLICY_DERIVE_ROWS = [
  { name: 'full', input: 'full', expected: 'full' },
  { name: 'incremental', input: 'incremental', expected: 'incremental' },
  { name: 'skip mapped', input: 'skip', expected: 'full' },
];
for (const { name, input, expected } of POLICY_DERIVE_ROWS) {
  test(`policy derivation: ${name}`, () => {
    const args = headlessArgs(input);
    delete args.reviewScope.requested;
    const before = structuredClone(args);
    assert.equal(normalizeArgs(args).reviewScope.requested, expected);
    assert.deepEqual(args, before);
  });
}

const SCOPE_DERIVE_ROWS = [
  { name: 'light', input: 'light', expected: { answer: 'light', drops: ['scopeAnswer'],
    paths: ['delivery.tier', 'scopeAnswer'], rejected: [], result: { ok: true, errors: [] } } },
  { name: 'full', input: 'full', expected: { answer: 'full', drops: ['scopeAnswer'],
    paths: ['delivery.tier', 'scopeAnswer'], rejected: [], result: { ok: true, errors: [] } } },
];
for (const { name, input, expected } of SCOPE_DERIVE_ROWS) {
  test(`scope derivation: ${name}`, () => {
    const args = headlessArgs();
    args.riskTable[0].risk = 'low';
    args.scopeAnswer = null;
    args.configEcho.trivial_scope.value = input;
    const report = normalizeArgsReport(args);
    assert.deepEqual({ answer: report.args.scopeAnswer, drops: report.dropped, paths: report.derivedPaths,
      rejected: nullToleranceRejectedKeys(report.args, report.dropped, report.derivedPaths), result: validateArgs(report.args) }, expected);
  });
}

const PROBE_ROWS = [
  { name: 'mapped accepts', mode: 'headless', input: { probeWaist: { field: 'mapped' } },
    descriptor: { key: 'lockstep_probe', waistPath: 'probeWaist.field', waistMap: { raw: 'mapped' },
      rule: { kind: 'enum', values: ['raw'] } }, receipt: 'raw', expected: { ok: true, errors: [] } },
  { name: 'raw rejects', mode: 'headless', input: { probeWaist: { field: 'raw' } },
    descriptor: { key: 'lockstep_probe', waistPath: 'probeWaist.field', waistMap: { raw: 'mapped' },
      rule: { kind: 'enum', values: ['raw'] } }, receipt: 'raw',
    expected: { ok: false, errors: ['probeWaist.field does not match configEcho.lockstep_probe'] } },
  { name: 'derived rejects', mode: 'interactive', input: {},
    descriptor: { key: 'derived_probe', modes: ['interactive'], allowedSources: { interactive: ['discovery'] },
      derivedFrom: 'reviewMd', rule: { kind: 'enum', values: ['present', 'absent'] } }, receipt: 'present', source: 'discovery',
    expected: { ok: false, errors: ['configEcho.derived_probe does not match reviewMd'] } },
  { name: 'unknown type', mode: 'headless', input: { boolProbe: { value: false } },
    descriptor: { key: 'bool_probe', type: 'bool', waistPath: 'boolProbe.value', rule: { kind: 'enum', values: ['true'] } },
    receipt: 'true', expected: { ok: true, errors: [] } },
  { name: 'CSV accepts', mode: 'headless', input: { csvProbe: { items: ['a', 'b'] } },
    descriptor: { key: 'csv_probe', type: 'csv_list', waistPath: 'csvProbe.items', rule: { kind: 'csv_subset', values: ['a', 'b'] } },
    receipt: 'a,b', expected: { ok: true, errors: [] } },
  { name: 'CSV reorders', mode: 'headless', input: { csvProbe: { items: ['b', 'a'] } },
    descriptor: { key: 'csv_probe', type: 'csv_list', waistPath: 'csvProbe.items', rule: { kind: 'csv_subset', values: ['a', 'b'] } },
    receipt: 'a,b', expected: { ok: false, errors: ['csvProbe.items does not match configEcho.csv_probe'] } },
  { name: 'CSV shortens', mode: 'headless', input: { csvProbe: { items: ['a'] } },
    descriptor: { key: 'csv_probe', type: 'csv_list', waistPath: 'csvProbe.items', rule: { kind: 'csv_subset', values: ['a', 'b'] } },
    receipt: 'a,b', expected: { ok: false, errors: ['csvProbe.items does not match configEcho.csv_probe'] } },
  { name: 'stamped null', mode: 'headless', input: { nullProbe: { value: null } },
    descriptor: { key: 'null_probe', waistPath: 'nullProbe.value' }, receipt: 'value',
    expected: { ok: false, errors: ['nullProbe.value does not match configEcho.null_probe'] } },
  { name: 'unstamped', mode: 'headless', input: {},
    descriptor: { key: 'null_probe', waistPath: 'nullProbe.value' }, receipt: 'value', expected: { ok: true, errors: [] } },
  { name: 'inactive mode', mode: 'interactive', input: { modeTwinProbe: { value: 'wrong' } },
    descriptor: { key: 'mode_twin_probe', waistPath: 'modeTwinProbe.value',
      allowedSources: { headless: ['default'], interactive: ['default'] } }, receipt: 'value',
    expected: { ok: false, errors: ['configEcho has unexpected key(s): mode_twin_probe'] } },
  { name: 'unknown own gate', mode: 'headless', input: { unknown: { field: 'wrong' } },
    descriptor: { key: 'unknown', waistPath: 'unknown.field', deriveWhen: 'nonsense' }, receipt: 'value',
    expected: { ok: true, errors: [] } },
  { name: 'inherited gate', mode: 'headless', input: { unknown: { field: 'wrong' } },
    descriptor: { key: 'unknown', waistPath: 'unknown.field', deriveWhen: 'toString' }, receipt: 'value',
    expected: { ok: true, errors: [] } },
];
for (const { name, mode, input, descriptor, receipt, source, expected } of PROBE_ROWS) {
  test(`copied table: ${name}`, () => {
    const args = { ...(mode === 'headless' ? headlessArgs() : interactiveArgs()), ...structuredClone(input) };
    args.configEcho[descriptor.key] = { value: receipt, source: source || 'default' };
    const table = knobRows({}, [{
      key: 'probe', modes: ['headless'], allowedSources: { headless: ['default'] }, rule: { kind: 'enum', values: ['value'] },
      env: null, reviewMdKey: null, defaults: { headless: ['value', 'default'] }, type: 'string', waistPath: null,
      waistMap: null, derivedFrom: null, deriveWhen: null, nullReceipt: [], resolvedKey: false, ...descriptor,
    }]);
    assert.deepEqual(validateArgs(args, table), expected);
  });
}

const TABLE_DERIVE_ROWS = [
  { name: 'scope gate copy', input: () => {
    const args = headlessArgs();
    args.configEcho.trivial_scope.value = 'light';
    return { args, table: knobRows({ trivial_scope: { deriveWhen: null } }), path: 'scopeAnswer' };
  }, expected: 'light' },
  { name: 'reviewed map copy', input: () => {
    const args = headlessArgs('skip');
    delete args.reviewScope.requested;
    return { args, table: knobRows({ reviewed_policy: { waistMap: { skip: 'incremental' } } }), path: 'reviewScope' };
  }, expected: { kind: 'full', since: null, commits: null, requested: 'incremental', detector: {
    previously_reviewed: true, sha_resolvable: true, head_advanced: true, sha_is_ancestor: true, incremental_safe: true, error: null,
  } } },
  { name: 'interactive modes copy', input: () => {
    const args = interactiveArgs();
    args.configEcho.reviewed_policy = { value: 'incremental', source: 'env' };
    args.reviewScope.detector = priorReviewDetector();
    delete args.reviewScope.requested;
    return { args, table: knobRows({ reviewed_policy: {
      modes: ['headless', 'interactive'], allowedSources: { headless: ['env', 'default'], interactive: ['env', 'default'] },
    } }), path: 'reviewScope' };
  }, expected: { kind: 'full', since: null, commits: null, requested: 'incremental', detector: {
    previously_reviewed: true, sha_resolvable: true, head_advanced: true, sha_is_ancestor: true, incremental_safe: true, error: null,
  } } },
];
for (const { name, input, expected } of TABLE_DERIVE_ROWS) {
  test(`copied derivation: ${name}`, () => {
    const { args, table, path } = input();
    const out = normalizeArgs(args, table);
    assert.deepEqual(out[path], expected);
    if (name === 'reviewed map copy' || name === 'interactive modes copy') {
      assert.deepEqual(validateArgs(out, table), { ok: true, errors: [] });
    }
  });
}

const UNKNOWN_DERIVE_ROWS = [
  { name: 'own gate', gate: 'nonsense', expected: { value: undefined, paths: ['delivery.tier'] } },
  { name: 'inherited gate', gate: 'toString', expected: { value: undefined, paths: ['delivery.tier'] } },
  { name: 'inactive mode', gate: null, modes: ['interactive'], expected: { value: undefined, paths: ['delivery.tier'] } },
  { name: 'unknown type', gate: null, type: 'bool', expected: { value: undefined, paths: ['delivery.tier'] } },
  { name: 'bad parent', gate: null, parent: [], expected: { value: undefined, paths: ['delivery.tier'] } },
  { name: 'scalar parent', gate: null, parent: 'nope', expected: { value: undefined, paths: ['delivery.tier'] } },
];
for (const { name, gate, modes, type, parent, expected } of UNKNOWN_DERIVE_ROWS) {
  test(`derive guard: ${name}`, () => {
    const args = headlessArgs();
    args.configEcho.probe = { value: 'value', source: 'default' };
    if (parent) args.probeWaist = parent;
    const table = knobRows({}, [{
      ...knobRows()[0], key: 'probe', modes: modes || ['headless'], type: type || 'string',
      allowedSources: { headless: ['default'] }, rule: { kind: 'enum', values: ['value'] },
      waistPath: 'probeWaist.field', deriveWhen: gate,
    }]);
    const out = normalizeArgsReport(args, table);
    assert.deepEqual({ value: out.args.probeWaist?.field, paths: out.derivedPaths }, expected);
  });
}

const NULL_PROBE_ROWS = [
  { name: 'two-level path', input: ['delivery.prIdentity.title'], derived: [], expected: ['delivery.prIdentity.title'] },
  { name: 'no dropped keys', input: [], derived: [], expected: [] },
  { name: 'bad dropped keys', input: null, derived: [], expected: [] },
  { name: 'bad derived paths', input: ['reviewConfig'], derived: null, expected: ['reviewConfig'] },
  { name: 'null identity rejected', input: ['delivery.prIdentity'], derived: [], expected: ['delivery.prIdentity'] },
  { name: 'derived drop', input: ['reviewConfig'], derived: ['reviewConfig'], expected: [] },
];
for (const { name, input, derived, expected } of NULL_PROBE_ROWS) {
  test(`null probe: ${name}`, () => {
    const args = interactiveArgs();
    const before = structuredClone(args);
    assert.deepEqual(nullToleranceRejectedKeys(args, input, derived), expected);
    assert.deepEqual(args, before);
  });
}

const RESOLVE_ROWS = [
  { name: 'none', input: undefined, expected: {
    reviewConfig: {}, exclusionPatterns: [], reviewConfigSource: 'none', exclusionsSource: 'none', reviewMdEntryCount: 0,
  } },
  { name: 'parsed axes', input: { reviewConfig: { ignore: ['a'] }, exclusionPatterns: ['b'] }, expected: {
    reviewConfig: { ignore: ['a'] }, exclusionPatterns: ['b'],
    reviewConfigSource: 'preParsed', exclusionsSource: 'preParsed', reviewMdEntryCount: 0,
  } },
  { name: 'empty raw axes override parsed', input: {
    reviewMd: [], exclusionsText: '', reviewConfig: { confidence_threshold: 99 }, exclusionPatterns: ['old'],
  }, expected: { reviewConfig: { ignore: [] }, exclusionPatterns: [],
    reviewConfigSource: 'reviewMd', exclusionsSource: 'exclusionsText', reviewMdEntryCount: 0 } },
  { name: 'raw root and child settings', input: { reviewMd: [
    { path: 'src/deep/REVIEW.md', text: '```yaml code-gauntlet\nconfidence_threshold: 90\nsecurity_min_confidence: 85\n' +
      'severity_threshold: high\nignore:\n  - c.js\n```' },
    { path: 'REVIEW.md', text: '```yaml code-gauntlet\nconfidence_threshold: 70\nsecurity_min_confidence: 75\n' +
      'severity_threshold: low\nignore:\n  - a.js\n  - b.js\n```' },
  ], exclusionsText: '- foo.js\n- bar/*.js\n' }, expected: {
    reviewConfig: { confidence_threshold: 70, security_min_confidence: 75, severity_threshold: 'low', ignore: ['a.js', 'b.js'],
      scopes: [{ dir: 'src/deep', confidence_threshold: 90, security_min_confidence: 85, severity_threshold: 'high', ignore: ['c.js'] }] },
    exclusionPatterns: ['foo.js', 'bar/*.js'], reviewConfigSource: 'reviewMd', exclusionsSource: 'exclusionsText', reviewMdEntryCount: 2,
  } },
  { name: 'raw review parsed exclusions', input: { reviewMd: [{ path: 'REVIEW.md', text: 'no config block here' }],
    exclusionPatterns: ['legacy.js'] }, expected: { reviewConfig: { ignore: [] }, exclusionPatterns: ['legacy.js'],
    reviewConfigSource: 'reviewMd', exclusionsSource: 'preParsed', reviewMdEntryCount: 1 } },
  { name: 'parsed review raw exclusions', input: { reviewConfig: { confidence_threshold: 70 }, exclusionsText: '- foo.js\n' },
    expected: { reviewConfig: { confidence_threshold: 70 }, exclusionPatterns: ['foo.js'],
      reviewConfigSource: 'preParsed', exclusionsSource: 'exclusionsText', reviewMdEntryCount: 0 } },
];
for (const { name, input, expected } of RESOLVE_ROWS) {
  test(`resolve config: ${name}`, () => assert.deepEqual(resolveReviewConfig(input), expected));
}

const GAP_ROWS = [
  { name: 'reviewConfig', input: 'reviewConfig', expected:
    "null_arg: args.reviewConfig arrived as a literal null and was treated as ABSENT \u2014 the review runs on " +
    "the Filter stage built-in thresholds (non-security 55, security 70) with no ignore list, NOT your " +
    "REVIEW.md configuration, so the delivered findings can differ. Omit reviewConfig entirely (or stamp a " +
    "well-formed value); do not stamp null." },
  { name: 'somethingNew', input: 'somethingNew', expected:
    "null_arg: args.somethingNew arrived as a literal null and was treated as ABSENT \u2014 the run proceeds as " +
    "if the field had not been supplied. Omit somethingNew entirely (or stamp a well-formed value); do not " +
    "stamp null." },
  { name: 'delivery.prIdentity.title', input: 'delivery.prIdentity.title', expected:
    "null_arg: args.delivery.prIdentity.title arrived as a literal null and was treated as ABSENT \u2014 the " +
    "report title falls back to owner/repo#N (or \"local changes\"). Omit delivery.prIdentity.title entirely " +
    "(or stamp a well-formed value); do not stamp null." },
  { name: 'respelling', input: 'pr_comment_cap', respell: true, expected:
    'null_receipt: args.pr_comment_cap arrived as a literal null and was spelled as the printed ' +
      'token "null"; the receipt and the configuration are unchanged; stamp the string "null".' },

];
for (const { name, input, respell, expected } of GAP_ROWS) {
  test(`gap: ${name}`, () => assert.equal(respell ? nullRespellGap(input) : nullToleranceGap(input), expected));
}

const VALIDATION_BOUNDARY_ROWS = [
  { name: 'zero cap mismatch', input: () => {
    const a = interactiveArgs(); a.limits.deliveryCap = 0; return a;
  }, expected: { ok: false, errors: ['limits.deliveryCap does not match configEcho.pr_comment_cap'] } },
  { name: 'invalid interactive cap receipt', input: () => {
    const a = interactiveArgs(); a.configEcho.pr_comment_cap.value = '00'; return a;
  }, expected: { ok: false, errors: ['configEcho.pr_comment_cap.value is invalid for interactive'] } },
  { name: 'invalid trivial receipt', input: () => {
    const a = headlessArgs(); a.configEcho.trivial_scope.value = 'none'; return a;
  }, expected: { ok: false, errors: ['configEcho.trivial_scope.value is invalid for headless'] } },
  { name: 'invalid derived receipt', input: () => {
    const a = interactiveArgs(); a.configEcho.review_md.value = 'other'; return a;
  }, expected: { ok: false, errors: ['configEcho.review_md.value is invalid for interactive'] } },
  { name: 'minimum context sizes', input: () => interactiveArgs({ contextLines: 1, contextChars: 1 }),
    expected: { ok: true, errors: [] } },
  { name: 'challenge integer domain', input: () => {
    const a = interactiveArgs(); a.limits.challengeCap = 1.5; return a;
  }, expected: { ok: false, errors: ['limits.challengeCap must be a non-negative safe integer when present'] } },
  { name: 'discovery integer domain', input: () => {
    const a = interactiveArgs(); a.limits.discoveryCap = 1.5; return a;
  }, expected: { ok: false, errors: ['limits.discoveryCap must be a positive safe integer when present'] } },
  { name: 'zero numeric receipt', input: () => {
    const a = interactiveArgs(); a.limits.deliveryCap = 0; a.configEcho.pr_comment_cap = { value: '0', source: 'env' }; return a;
  }, expected: { ok: true, errors: [] } },
  { name: 'scope trailing slash', input: () => interactiveArgs({ reviewConfig: { scopes: [{ dir: 'api/' }] } }),
    expected: { ok: false, errors: ['reviewConfig.scopes[0].dir must not have a leading or trailing /',
      'reviewConfig.scopes[0].dir must not contain an empty path segment'] } },
  { name: 'review path dot-dot', input: () => {
    const a = interactiveArgs({ reviewMd: [{ path: 'api/../REVIEW.md', text: '' }] });
    a.configEcho.review_md.value = 'present'; return a;
  }, expected: { ok: false, errors: ['reviewMd[0].path must not contain . or .. path segments'] } },
  { name: 'scope path dot-dot', input: () => interactiveArgs({ reviewConfig: { scopes: [{ dir: 'api/../src' }] } }),
    expected: { ok: false, errors: ['reviewConfig.scopes[0].dir must not contain . or .. path segments'] } },
  { name: 'absent tier in empty delivery', input: () => {
    const a = interactiveArgs({ delivery: {} }); a.configEcho.delivery_tier.value = 'main_only'; return a;
  }, expected: { ok: false, errors: ['configEcho.delivery_tier does not match delivery.tier'] } },
  { name: 'PR comments complete identity', input: () => {
    const a = headlessArgs();
    a.configEcho.delivery.value = 'chat,pr_comments,markdown';
    a.delivery.prIdentity = { owner: 'o', repo: 'r', pr_number: 1, sha_full: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      platform: 'github', web_origin: 'https://github.com' };
    return a;
  }, expected: { ok: true, errors: [] } },
];
for (const { name, input, expected } of VALIDATION_BOUNDARY_ROWS) {
  test(`validate boundary: ${name}`, () => assert.deepEqual(validateArgs(input()), expected));
}
