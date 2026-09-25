require "base64"
require "digest"
require "json"

EXPECTED_GITLAB_VERSION = "19.4.1"
RESULT_SENTINEL = "GITLAB_QUICK_ACTION_VERDICTS_JSON:"
CASES_B64 = "__CASES_B64__"

version = Gitlab::VERSION.to_s
abort("Expected GitLab #{EXPECTED_GITLAB_VERSION}, got #{version}") unless version == EXPECTED_GITLAB_VERSION

cases = JSON.parse(Base64.decode64(CASES_B64))
extractor = Gitlab::QuickActions::Extractor.new(
  QuickActions::InterpretService.command_definitions
)
results = cases.map do |item|
  text = item.fetch("text")
  paragraphs = Banzai.render_result(text, { pipeline: :quick_action })[:quick_action_paragraphs]
  content, commands = extractor.extract_commands(text)
  normalized_posted = text.delete("\r").rstrip
  verdict = {
    "id" => item.fetch("id"),
    "group" => item.fetch("group"),
    "sha256" => Digest::SHA256.hexdigest(text.encode(Encoding::UTF_8)),
    "paragraphs" => paragraphs,
    "commands" => commands,
    "stored_equals_posted" => content == normalized_posted
  }
  verdict["content"] = content unless content == normalized_posted
  verdict
end

puts "#{RESULT_SENTINEL}#{JSON.generate({ "gitlab_version" => version, "cases" => results })}"
