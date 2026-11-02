import { canonicalRange } from './location.js';
import { WIRE_SPACE, WS_TRIM_RE, asText, asConfidence, deepFreeze } from './wire.js';
import { INJECTION_STRIPPED_PROSE_FIELDS } from './registry.js';

export const INJECTION_TITLE_PATTERNS = deepFreeze([
  new RegExp(String.raw`\bExample${WIRE_SPACE}+finding\b`, 'i'),
  new RegExp(String.raw`\bSample${WIRE_SPACE}+finding\b`, 'i'),
  new RegExp(String.raw`\btest${WIRE_SPACE}+finding\b`, 'i'),
  new RegExp(String.raw`\bdemo${WIRE_SPACE}+finding\b`, 'i'),
  new RegExp(String.raw`\bPlaceholder${WIRE_SPACE}+finding\b`, 'i'),
]);

const INJECTION_BODY_PATTERNS = deepFreeze([
  new RegExp(String.raw`<finding(?:${WIRE_SPACE}[^>]*)?>`, 'i'),
  new RegExp(String.raw`<example(?:${WIRE_SPACE}[^>]*)?>`, 'i'),
  new RegExp(String.raw`\[${WIRE_SPACE}*INSERT${WIRE_SPACE}*\]`, 'i'),
  new RegExp(String.raw`\[${WIRE_SPACE}*INSERT\b[^\]]*\b(?:FINDING|TITLE|TEXT|PLACEHOLDER|HERE)\b[^\]]*\]`, 'i'),
  new RegExp(String.raw`lorem${WIRE_SPACE}+ipsum`, 'i'),
]);

const INJECTION_SHELL_PATTERNS = deepFreeze([
  new RegExp(String.raw`\brm${WIRE_SPACE}+-[rf]`, 'i'),
  new RegExp(String.raw`\bcurl${WIRE_SPACE}+https?:\/\/`, 'i'),
  new RegExp(String.raw`\bwget${WIRE_SPACE}+https?:\/\/`, 'i'),
  new RegExp(String.raw`\bgit${WIRE_SPACE}+push\b`, 'i'),
  new RegExp(String.raw`\bgh${WIRE_SPACE}+api\b`, 'i'),
]);

const INJECTION_URL_PATTERNS = deepFreeze([
  new RegExp(String.raw`\bvisit${WIRE_SPACE}+[a-z][a-z0-9+.\-]{1,15}:\/\/`, 'i'),
  new RegExp(String.raw`\bdownload${WIRE_SPACE}+from${WIRE_SPACE}+[a-z][a-z0-9+.\-]{1,15}:\/\/`, 'i'),
]);

// Require a decode directive or execution sink near encoded text so ordinary hashes and tokens stay inert.
const INJECTION_ENCODED_PATTERNS = deepFreeze([
  /\b(?:decode|base64|atob|b64decode)\b[^\x00]{0,40}[A-Za-z0-9+\/]{40,}={0,2}\b/i,
  new RegExp(String.raw`\b[A-Za-z0-9+\/]{40,}={0,2}\b[^\x00]{0,40}(?:\|${WIRE_SPACE}*(?:sh|bash|zsh)\b|base64${WIRE_SPACE}+-d\b|(?:then|and)${WIRE_SPACE}+(?:run|execute|eval)\b)`, 'i'),
  /\b(?:decode|unhex|xxd|fromhex|unhexlify)\b[^\x00]{0,40}(?<!\w)(?:0x)?[0-9a-fA-F]{32,}(?!\w)/i,
  new RegExp(String.raw`(?<!\w)(?:0x)?[0-9a-fA-F]{32,}(?!\w)[^\x00]{0,40}(?:\|${WIRE_SPACE}*(?:xxd|sh|bash)\b|(?:then|and)${WIRE_SPACE}+(?:run|execute|eval)\b|-r${WIRE_SPACE}+-p\b)`, 'i'),
]);

// Gate auto-approve on imperative grammar so third-person policy prose is not treated as an instruction.
const INJECTION_BYPASS_PATTERNS = deepFreeze([
  new RegExp(String.raw`\bskip${WIRE_SPACE}+review\b`, 'i'),
  new RegExp(String.raw`\bauto[-\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]?approve${WIRE_SPACE}+(?:this|these|the|it|my|your)${WIRE_SPACE}+(?:pr|pull${WIRE_SPACE}+request|mr|merge${WIRE_SPACE}+request|changes?|commit)\b`, 'i'),
  new RegExp(String.raw`\bauto[-\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]?approve${WIRE_SPACE}+and${WIRE_SPACE}+(?:merge|skip|bypass|push|deploy|proceed|continue)\b`, 'i'),
  new RegExp(String.raw`\bbypass${WIRE_SPACE}+(?:security${WIRE_SPACE}+)?controls?\b`, 'i'),
  new RegExp(String.raw`\bbypass${WIRE_SPACE}+(?:the${WIRE_SPACE}+)?(?:auth|authentication|authorization)\b`, 'i'),
  new RegExp(String.raw`\bdisable${WIRE_SPACE}+(?:auth|authentication|authorization)\b`, 'i'),
  new RegExp(String.raw`\bmark${WIRE_SPACE}+(?:this${WIRE_SPACE}+)?(?:finding${WIRE_SPACE}+)?as${WIRE_SPACE}+safe\b`, 'i'),
  new RegExp(String.raw`\bapprove${WIRE_SPACE}+(?:this|the)${WIRE_SPACE}+(?:PR|pull${WIRE_SPACE}+request|change)\b`, 'i'),
]);

const INJECTION_INSTRUCTIONAL_PATTERNS = deepFreeze([
  new RegExp(String.raw`\byou${WIRE_SPACE}+should${WIRE_SPACE}+run\b`, 'i'),
  new RegExp(String.raw`\bexecute${WIRE_SPACE}+the${WIRE_SPACE}+following\b`, 'i'),
  new RegExp(String.raw`\brun${WIRE_SPACE}+this${WIRE_SPACE}+command\b`, 'i'),
  new RegExp(String.raw`\bplease${WIRE_SPACE}+run\b`, 'i'),
  new RegExp(String.raw`\bpaste${WIRE_SPACE}+(?:this|the${WIRE_SPACE}+following)${WIRE_SPACE}+into${WIRE_SPACE}+(?:your${WIRE_SPACE}+)?terminal`, 'i'),
  new RegExp(String.raw`\bcopy${WIRE_SPACE}+and${WIRE_SPACE}+paste${WIRE_SPACE}+the${WIRE_SPACE}+following\b`, 'i'),
]);

const INJECTION_VULN_INTRO_PATTERNS = deepFreeze([
  new RegExp(String.raw`\badd${WIRE_SPACE}+eval${WIRE_SPACE}*\(`, 'i'),
  new RegExp(String.raw`\buse${WIRE_SPACE}+eval${WIRE_SPACE}*\(`, 'i'),
  new RegExp(String.raw`\bdisable${WIRE_SPACE}+(?:CORS|CSP|content[-\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]security[-\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]policy)\b`, 'i'),
  new RegExp(String.raw`\bdisable${WIRE_SPACE}+(?:CSRF|csrf)${WIRE_SPACE}+(?:protection|check|token)\b`, 'i'),
  new RegExp(String.raw`\ballow${WIRE_SPACE}+all${WIRE_SPACE}+origins\b`, 'i'),
  new RegExp(String.raw`\bset${WIRE_SPACE}+secure${WIRE_SPACE}+to${WIRE_SPACE}+false\b`, 'i'),
  new RegExp(String.raw`\bdisable${WIRE_SPACE}+(?:TLS|SSL|HTTPS)${WIRE_SPACE}+(?:verification|validation)\b`, 'i'),
  new RegExp(String.raw`\bskip${WIRE_SPACE}+(?:certificate|cert)${WIRE_SPACE}+(?:verification|validation)\b`, 'i'),
  new RegExp(String.raw`\bdisable${WIRE_SPACE}+security${WIRE_SPACE}+(?:check|feature|control)\b`, 'i'),
]);

const MIN_BODY_WORDS = 10;

const HIGH_CONFIDENCE_THRESHOLD = 85;

export const WORD_SPLIT_RE = new RegExp(String.raw`${WIRE_SPACE}+`);

export function countWords(text) {
  return (text || '').split(WORD_SPLIT_RE).filter(Boolean).length;
}

// Packed Unicode tables are decoded once; explicit mappings avoid runtime normalization drift.
const CONFUSABLE_FOLD_PACKED =
  '\u00aaa\u00bao\u00d7x\u00fep\u0130i\u0131i\u017fs\u0184b\u018dg\u0192f\u0196l\u01a6R\u01bds\u01bfp\u01c0l\u0251a\u0261g\u0263y\u0269i' +
  '\u026ai\u026fw\u028bu\u028fy\u02b0h\u02b2j\u02b3r\u02b7w\u02b8y\u02dbi\u02e1l\u02e2s\u02e3x\u037ai\u037fJ\u0391A\u0392B\u0395E\u0396Z' +
  '\u0397H\u0399l\u039aK\u039cM\u039dN\u039fO\u03a1P\u03a4T\u03a5Y\u03a7X\u03b1a\u03b3y\u03b9i\u03bdv\u03bfo\u03c1p\u03c3o\u03c5u\u03d2Y' +
  '\u03dcF\u03edo\u03f1p\u03f2c\u03f3j\u03f8p\u03f9C\u03faM\u0405S\u0406l\u0408J\u0410A\u0412B\u0415E\u041aK\u041cM\u041dH\u041eO\u0420P' +
  '\u0421C\u0422T\u0423Y\u0425X\u042cb\u0430a\u0433r\u0435e\u043eo\u0440p\u0441c\u0443y\u0445x\u0448w\u0455s\u0456i\u0458j\u0461w\u0474V' +
  '\u0475v\u04aeY\u04afy\u04bbh\u04bde\u04c0l\u04cfl\u0501d\u050cG\u051bq\u051cW\u051dw\u054dU\u054fS\u0555O\u0561w\u0563q\u0566q\u0570h' +
  '\u0578n\u057cn\u057du\u0581g\u0582i\u0584f\u0585o\u05c0l\u05d5l\u05d8v\u05dfl\u05e1o\u0627l\u0647o\u0661l\u0665o\u0667V\u06beo\u06c1o' +
  '\u06d5o\u06f1l\u06f5o\u06f7V\u07c0O\u07cal\u0966o\u09e6o\u0a66o\u0ae6o\u0b20O\u0b66o\u0be6o\u0c02o\u0c66o\u0c82o\u0ce6O\u0d02o\u0d1fs' +
  '\u0d20o\u0d66o\u0d82o\u0e50o\u0ed0o\u1004c\u101do\u1040o\u105ac\u10e7y\u10ffo\u1200U\u12d0O\u13a0D\u13a1R\u13a2T\u13a5i\u13a9Y\u13aaA' +
  '\u13abJ\u13acE\u13b3W\u13b7M\u13bbH\u13bdY\u13c0G\u13c2h\u13c3Z\u13cfb\u13d2R\u13d4W\u13d5S\u13d9V\u13daS\u13deL\u13dfC\u13e2P\u13e6K' +
  '\u13e7d\u13f3G\u13f4B\u142fV\u144cU\u146dP\u146fd\u1472b\u148dJ\u14aaL\u1541x\u157cH\u157dx\u1587R\u15afb\u15b4F\u15c5A\u15deD\u15eaD' +
  '\u15f0M\u15f7B\u166dX\u166ex\u16b7X\u16c1l\u16d5K\u16d6M\u17e0o\u1d04c\u1d0fo\u1d11o\u1d1cu\u1d20v\u1d21w\u1d22z\u1d26r\u1d2cA\u1d2eB' +
  '\u1d30D\u1d31E\u1d33G\u1d34H\u1d35I\u1d36J\u1d37K\u1d38L\u1d39M\u1d3aN\u1d3cO\u1d3eP\u1d3fR\u1d40T\u1d41U\u1d42W\u1d43a\u1d47b\u1d48d' +
  '\u1d49e\u1d4dg\u1d4fk\u1d50m\u1d52o\u1d56p\u1d57t\u1d58u\u1d5bv\u1d62i\u1d63r\u1d64u\u1d65v\u1d83g\u1d8cy\u1d9cc\u1da0f\u1dbbz\u1e9df' +
  '\u1effy\u1fbei\u2071i\u207fn\u2090a\u2091e\u2092o\u2093x\u2095h\u2096k\u2097l\u2098m\u2099n\u209ap\u209bs\u209ct\u2102C\u210ag\u210bH' +
  '\u210cH\u210dH\u210eh\u2110I\u2111I\u2112L\u2113l\u2115N\u2119P\u211aQ\u211bR\u211cR\u211dR\u2124Z\u2128Z\u212ak\u212cB\u212dC\u212ee' +
  '\u212fe\u2130E\u2131F\u2133M\u2134o\u2139i\u213dy\u2145D\u2146d\u2147e\u2148i\u2149j\u2160I\u2164V\u2169X\u216cL\u216dC\u216eD\u216fM' +
  '\u2170i\u2174v\u2179x\u217cl\u217dc\u217ed\u217fm\u2223l\u2228v\u222aU\u22a4T\u22c1v\u22c3U\u22ffE\u2373i\u2374p\u237aa\u23fdl\u2573X' +
  '\u27d9T\u292bx\u292cx\u2a2fx\u2c7cj\u2c7dV\u2c82B\u2c85r\u2c8eH\u2c92l\u2c93i\u2c94K\u2c98M\u2c9aN\u2c9eO\u2c9fo\u2ca2P\u2ca3p\u2ca4C' +
  '\u2ca5c\u2ca6T\u2ca8Y\u2ca9y\u2cacX\u2cbdw\u2cceP\u2ccfp\u2cd0L\u2d38V\u2d39E\u2d4fl\u2d54O\u2d55Q\u2d5dX\u3007O\ua4d0B\ua4d1P\ua4d2d' +
  '\ua4d3D\ua4d4T\ua4d6G\ua4d7K\ua4d9J\ua4daC\ua4dcZ\ua4ddF\ua4dfM\ua4e0N\ua4e1L\ua4e2S\ua4e3R\ua4e6V\ua4e7H\ua4eaW\ua4ebX\ua4ecY\ua4eeA' +
  '\ua4f0E\ua4f2l\ua4f3O\ua4f4U\ua647i\ua6dfV\ua731s\ua798F\ua799f\ua79fu\ua7b2J\ua7b3X\ua7b4B\ua7f2C\ua7f3F\ua7f4Q\uab32e\uab35f\uab3do' +
  '\uab47r\uab48r\uab4eu\uab52u\uab5ay\uab75i\uab81r\uab83w\uab93z\uaba9v\uabaas\uabafc\ufba6o\ufba7o\ufba8o\ufba9o\ufbaao\ufbabo\ufbaco' +
  '\ufbado\ufe8dl\ufe8el\ufee9o\ufeeao\ufeebo\ufeeco\uffe8l\u{10282}B\u{10286}E\u{10287}F\u{1028a}l\u{10290}X\u{10292}O\u{10295}P' +
  '\u{10296}S\u{10297}T\u{102a0}A\u{102a1}B\u{102a2}C\u{102a5}F\u{102ab}O\u{102b0}M\u{102b1}T\u{102b2}Y\u{102b4}X\u{102cf}H\u{102f5}Z' +
  '\u{10301}B\u{10302}C\u{10309}l\u{10311}M\u{10315}T\u{10317}X\u{10320}l\u{10322}X\u{10404}O\u{10415}C\u{1041b}L\u{10420}S\u{1042c}o' +
  '\u{1043d}c\u{10448}s\u{104b4}R\u{104c2}O\u{104ce}U\u{104ea}o\u{104f6}u\u{10513}N\u{10516}O\u{10518}K\u{1051c}C\u{1051d}V\u{10525}F' +
  '\u{10526}L\u{10527}X\u{107a5}q\u{114d0}o\u{11706}v\u{1170a}w\u{1170e}w\u{1170f}w\u{118a0}V\u{118a2}F\u{118a3}L\u{118a4}Y\u{118a6}E' +
  '\u{118a9}Z\u{118ae}E\u{118b2}L\u{118b5}O\u{118b8}U\u{118bc}T\u{118c0}v\u{118c1}s\u{118c2}F\u{118c3}i\u{118c4}z\u{118c8}o\u{118d7}o' +
  '\u{118d8}u\u{118dc}y\u{118e0}O\u{118e5}Z\u{118e6}W\u{118e9}C\u{118ec}X\u{118ef}W\u{118f2}C\u{11dda}l\u{11de0}O\u{11de1}l\u{16eaa}l' +
  '\u{16eb6}b\u{16f08}V\u{16f0a}T\u{16f16}L\u{16f28}l\u{16f35}R\u{16f3a}S\u{16f40}A\u{16f42}U\u{16f43}Y\u{1ccde}l\u{1ccf0}O\u{1ccf1}l' +
  '\u{1d20d}V\u{1d213}F\u{1d216}R\u{1d22a}L\u{1d44e}a\u{1d44f}b\u{1d450}c\u{1d451}d\u{1d452}e\u{1d453}f\u{1d454}g\u{1d456}i\u{1d457}j' +
  '\u{1d458}k\u{1d459}l\u{1d45a}m\u{1d45b}n\u{1d45c}o\u{1d45d}p\u{1d45e}q\u{1d45f}r\u{1d460}s\u{1d461}t\u{1d462}u\u{1d463}v\u{1d464}w' +
  '\u{1d465}x\u{1d466}y\u{1d467}z\u{1d49c}A\u{1d49e}C\u{1d49f}D\u{1d4a2}G\u{1d4a5}J\u{1d4a6}K\u{1d4a9}N\u{1d4aa}O\u{1d4ab}P\u{1d4ac}Q' +
  '\u{1d4ae}S\u{1d4af}T\u{1d4b0}U\u{1d4b1}V\u{1d4b2}W\u{1d4b3}X\u{1d4b4}Y\u{1d4b5}Z\u{1d4b6}a\u{1d4b7}b\u{1d4b8}c\u{1d4b9}d\u{1d4bb}f' +
  '\u{1d4bd}h\u{1d4be}i\u{1d4bf}j\u{1d4c0}k\u{1d4c1}l\u{1d4c2}m\u{1d4c3}n\u{1d4c5}p\u{1d4c6}q\u{1d4c7}r\u{1d4c8}s\u{1d4c9}t\u{1d4ca}u' +
  '\u{1d4cb}v\u{1d4cc}w\u{1d4cd}x\u{1d4ce}y\u{1d4cf}z\u{1d504}A\u{1d505}B\u{1d507}D\u{1d508}E\u{1d509}F\u{1d50a}G\u{1d50d}J\u{1d50e}K' +
  '\u{1d50f}L\u{1d510}M\u{1d511}N\u{1d512}O\u{1d513}P\u{1d514}Q\u{1d516}S\u{1d517}T\u{1d518}U\u{1d519}V\u{1d51a}W\u{1d51b}X\u{1d51c}Y' +
  '\u{1d538}A\u{1d539}B\u{1d53b}D\u{1d53c}E\u{1d53d}F\u{1d53e}G\u{1d540}I\u{1d541}J\u{1d542}K\u{1d543}L\u{1d544}M\u{1d546}O\u{1d54a}S' +
  '\u{1d54b}T\u{1d54c}U\u{1d54d}V\u{1d54e}W\u{1d54f}X\u{1d550}Y\u{1d6a4}i\u{1d6a8}A\u{1d6a9}B\u{1d6ac}E\u{1d6ad}Z\u{1d6ae}H\u{1d6b0}l' +
  '\u{1d6b1}K\u{1d6b3}M\u{1d6b4}N\u{1d6b6}O\u{1d6b8}P\u{1d6bb}T\u{1d6bc}Y\u{1d6be}X\u{1d6c2}a\u{1d6c4}y\u{1d6ca}i\u{1d6ce}v\u{1d6d0}o' +
  '\u{1d6d2}p\u{1d6d4}o\u{1d6d6}u\u{1d6e0}p\u{1d6e2}A\u{1d6e3}B\u{1d6e6}E\u{1d6e7}Z\u{1d6e8}H\u{1d6ea}l\u{1d6eb}K\u{1d6ed}M\u{1d6ee}N' +
  '\u{1d6f0}O\u{1d6f2}P\u{1d6f5}T\u{1d6f6}Y\u{1d6f8}X\u{1d6fc}a\u{1d6fe}y\u{1d704}i\u{1d708}v\u{1d70a}o\u{1d70c}p\u{1d70e}o\u{1d710}u' +
  '\u{1d71a}p\u{1d71c}A\u{1d71d}B\u{1d720}E\u{1d721}Z\u{1d722}H\u{1d724}l\u{1d725}K\u{1d727}M\u{1d728}N\u{1d72a}O\u{1d72c}P\u{1d72f}T' +
  '\u{1d730}Y\u{1d732}X\u{1d736}a\u{1d738}y\u{1d73e}i\u{1d742}v\u{1d744}o\u{1d746}p\u{1d748}o\u{1d74a}u\u{1d754}p\u{1d756}A\u{1d757}B' +
  '\u{1d75a}E\u{1d75b}Z\u{1d75c}H\u{1d75e}l\u{1d75f}K\u{1d761}M\u{1d762}N\u{1d764}O\u{1d766}P\u{1d769}T\u{1d76a}Y\u{1d76c}X\u{1d770}a' +
  '\u{1d772}y\u{1d778}i\u{1d77c}v\u{1d77e}o\u{1d780}p\u{1d782}o\u{1d784}u\u{1d78e}p\u{1d790}A\u{1d791}B\u{1d794}E\u{1d795}Z\u{1d796}H' +
  '\u{1d798}l\u{1d799}K\u{1d79b}M\u{1d79c}N\u{1d79e}O\u{1d7a0}P\u{1d7a3}T\u{1d7a4}Y\u{1d7a6}X\u{1d7aa}a\u{1d7ac}y\u{1d7b2}i\u{1d7b6}v' +
  '\u{1d7b8}o\u{1d7ba}p\u{1d7bc}o\u{1d7be}u\u{1d7c8}p\u{1d7ca}F\u{1d7ce}O\u{1d7cf}l\u{1d7d8}O\u{1d7d9}l\u{1d7e2}O\u{1d7e3}l\u{1d7ec}O' +
  '\u{1d7ed}l\u{1d7f6}O\u{1d7f7}l\u{1e8c7}l\u{1ee00}l\u{1ee24}o\u{1ee64}o\u{1ee80}l\u{1ee84}o\u{1f12b}C\u{1f12c}R\u{1f74c}C\u{1f768}T' +
  '\u{1fbf0}O\u{1fbf1}l';

// Complete alphabets share one mapping; irregular folds remain explicit above.
const CONFUSABLE_ALPHABETS = [
  ['ABCDEFGHIJKLMNOPQRSTUVWXYZ', [0x24b6, 0xff21, 0x1d400, 0x1d434, 0x1d468, 0x1d4d0, 0x1d56c, 0x1d5a0, 0x1d5d4, 0x1d608, 0x1d63c, 0x1d670, 0x1f130]],
  ['abcdefghijklmnopqrstuvwxyz', [0x24d0, 0xff41, 0x1d41a, 0x1d482, 0x1d4ea, 0x1d51e, 0x1d552, 0x1d586, 0x1d5ba, 0x1d5ee, 0x1d622, 0x1d656, 0x1d68a]],
];

// Explicit ranges keep membership stable across runtime Unicode versions.
const INVISIBLE_STRIP_RANGES = [
  [0xad, 0xad],
  [0x300, 0x36f],
  [0x61c, 0x61c],
  [0x180e, 0x180e],
  [0x1ab0, 0x1aff],
  [0x1dc0, 0x1dff],
  [0x200b, 0x200f],
  [0x202a, 0x202e],
  [0x2060, 0x2064],
  [0x2066, 0x2069],
  [0x20d0, 0x20ff],
  [0xfe00, 0xfe0f],
  [0xfe20, 0xfe2f],
  [0xfeff, 0xfeff],
  [0xe0100, 0xe01ef],
];

// Decode full code points so astral entries do not split into surrogate halves.
export const CONFUSABLE_FOLD = new Map();
{
  const cps = [...CONFUSABLE_FOLD_PACKED];
  const entries = [];
  for (let i = 0; i < cps.length; i += 2) entries.push([cps[i].codePointAt(0), cps[i + 1]]);
  for (const [alphabet, starts] of CONFUSABLE_ALPHABETS) {
    for (const start of starts) {
      for (let i = 0; i < alphabet.length; i++) entries.push([start + i, alphabet[i]]);
    }
  }
  // Preserve the public map's code-point order after expanding the shared alphabets.
  for (const [cp, folded] of entries.sort((a, b) => a[0] - b[0])) CONFUSABLE_FOLD.set(cp, folded);
}
deepFreeze(CONFUSABLE_FOLD);

export const INVISIBLE_STRIP = deepFreeze(new Set(
  INVISIBLE_STRIP_RANGES.flatMap(([first, last]) => Array.from({ length: last - first + 1 }, (_, index) => first + index)),
));

export function foldConfusables(text) {
  let out = '';
  for (const ch of text) {
    const cp = ch.codePointAt(0);
    if (INVISIBLE_STRIP.has(cp)) continue;
    out += CONFUSABLE_FOLD.get(cp) ?? ch;
  }
  return out;
}

// Prefer raw matches so diagnostics name the original trigger before any Unicode-folded trigger.
function firstMatch(text, folded, patterns) {
  for (const rx of patterns) {
    if (rx.test(text)) return rx.source;
  }
  if (folded !== text) {
    for (const rx of patterns) {
      if (rx.test(folded)) return rx.source;
    }
  }
  return null;
}

export const INJECTION_RULES = deepFreeze([
  { kind: 'pattern', domain: 'combined', label: 'contains shell command pattern',
    patterns: INJECTION_SHELL_PATTERNS, prose: true },
  { kind: 'pattern', domain: 'combined', label: 'contains visit-URL pattern',
    patterns: INJECTION_URL_PATTERNS, prose: true },
  { kind: 'pattern', domain: 'combined', label: 'contains encoded payload pattern',
    patterns: INJECTION_ENCODED_PATTERNS, prose: true },
  { kind: 'pattern', domain: 'combined', label: 'contains bypass/auto-approve instruction',
    patterns: INJECTION_BYPASS_PATTERNS, prose: true },
  { kind: 'shortBody', minimumWords: MIN_BODY_WORDS, confidence: HIGH_CONFIDENCE_THRESHOLD,
    label: 'suspiciously short description ({words} words) with high confidence ({confidence})' },
  { kind: 'pattern', domain: 'combined', label: 'uses instructional tone',
    patterns: INJECTION_INSTRUCTIONAL_PATTERNS, prose: true },
  { kind: 'pattern', domain: 'combined', label: 'recommends introducing vulnerability',
    patterns: INJECTION_VULN_INTRO_PATTERNS, prose: true },
  { kind: 'pattern', domain: 'title', label: 'title matches placeholder pattern', patterns: INJECTION_TITLE_PATTERNS },
  { kind: 'pattern', domain: 'combined', label: 'matches injection marker', patterns: INJECTION_BODY_PATTERNS, prose: true },
  { kind: 'path', pattern: /<[^\n]*?>|\{[^\n]*?\}/, label: 'file path is empty or contains template markers' },
  { kind: 'duplicate', label: 'duplicate of finding' },
]);

// Reject oversized fixes before delivery can try to render a suggestion fence.
export const FIX_MAX_LINES = 100;

export const FIX_MAX_CHARS = 8000;

// Discount only one terminal LF when measuring delivery bounds; surrounding fences remain part of the code.
function normalizeFixCodeForBound(code) {
  return code.endsWith('\n') ? code.slice(0, -1) : code;
}

function stripInjectionField(finding, field, reason) {
  const kept = { ...finding };
  delete kept[field];
  kept[`${field}_removed_by`] = 'injection';
  kept[`${field}_removal_reason`] = reason;
  return kept;
}

function stripInjectedProseFields(finding, rules) {
  let kept = finding;
  let firstPatternStrip = null;
  for (const field of INJECTION_STRIPPED_PROSE_FIELDS) {
    if (field in kept && typeof kept[field] !== 'string') {
      kept = stripInjectionField(kept, field, `${field} is not a string`);
      continue;
    }
    const value = typeof kept[field] === 'string' ? kept[field] : '';
    if (!value) continue;
    const valueFolded = foldConfusables(value);
    for (const row of rules) {
      if (!row.prose) continue;
      const m = firstMatch(value, valueFolded, row.patterns);
      if (m) {
        kept = stripInjectionField(kept, field, `${field} ${row.label}: ${JSON.stringify(m)}`);
        // First field and rule win so propagated fix-removal reasons remain deterministic.
        if (firstPatternStrip === null) firstPatternStrip = [field, row.label];
        break;
      }
    }
  }
  return [kept, firstPatternStrip];
}

function stripSuggestedFixCodeIfNeeded(finding, firstPatternStrip) {
  if (!('suggested_fix_code' in finding)) return finding;
  const code = finding.suggested_fix_code;
  if (typeof code !== 'string') {
    return stripInjectionField(finding, 'suggested_fix_code', 'suggested_fix_code is not a string');
  }
  const normalized = normalizeFixCodeForBound(code);
  if (normalized.split('\n').length > FIX_MAX_LINES || [...normalized].length > FIX_MAX_CHARS) {
    return stripInjectionField(finding, 'suggested_fix_code', 'suggested_fix_code exceeds the delivery bound');
  }
  if (firstPatternStrip !== null) {
    // Stripped prose taints its accompanying fix even when the code itself passes the delivery bounds.
    const [propField, propPhrase] = firstPatternStrip;
    return stripInjectionField(finding, 'suggested_fix_code', `${propField} carried ${propPhrase}`);
  }
  return finding;
}

export function applyInjectedProseStrip(finding, rules = INJECTION_RULES) {
  const [stripped, firstPatternStrip] = stripInjectedProseFields(finding, rules);
  return stripSuggestedFixCodeIfNeeded(stripped, firstPatternStrip);
}

// Rule order fixes diagnostic reason order; replay scans stage outputs without reapplying discovery's short-body heuristic.
function injectionScanCore(findings, includeH4, rules) {
  const kept = [];
  const eliminated = [];
  const seenSignatures = new Map();

  for (const finding of findings) {
    const title = asText(finding.title);
    const description = asText(finding.description);
    const filepath = asText(finding.file);
    const confidence = asConfidence(finding.confidence);
    const combined = `${title}\n${description}`;
    const combinedFolded = foldConfusables(combined);
    const titleFolded = foldConfusables(title);

    const reasons = [];

    for (const row of rules) {
      if (row.kind === 'pattern') {
        const raw = row.domain === 'title' ? title : combined;
        const folded = row.domain === 'title' ? titleFolded : combinedFolded;
        const m = firstMatch(raw, folded, row.patterns);
        if (m) reasons.push(`${row.label}: ${JSON.stringify(m)}`);
      } else if (row.kind === 'shortBody' && includeH4) {
        const wordCount = countWords(description);
        if (wordCount < row.minimumWords && confidence >= row.confidence) {
          reasons.push(row.label.replace('{words}', wordCount).replace('{confidence}', confidence));
        }
      } else if (row.kind === 'path') {
        if (!filepath || row.pattern.test(filepath)) {
          reasons.push(`${row.label}: ${JSON.stringify(filepath)}`);
        }
      } else if (row.kind === 'duplicate') {
        // Ordered tuple encoding keeps title and location boundaries unambiguous for duplicate detection.
        const sig = JSON.stringify([title.toLowerCase().replace(WS_TRIM_RE, ''), filepath, canonicalRange(finding) ? ['resolved', finding.line_start] : ['unconfirmed', finding.id]]);
        if (seenSignatures.has(sig)) {
          reasons.push(`${row.label} ${JSON.stringify(seenSignatures.get(sig))}`);
        } else {
          seenSignatures.set(sig, finding.id !== undefined && finding.id !== null ? finding.id : title);
        }
      }
    }

    if (reasons.length) {
      // Preserve eliminated prose as forensic input; only survivors receive field stripping.
      const elim = { ...finding, eliminated_by: 'injection', elimination_reason: reasons.join('; ') };
      eliminated.push(elim);
    } else {
      kept.push(applyInjectedProseStrip(finding, rules));
    }
  }

  return { kept, eliminated };
}

export function applyInjectionFilter(findings, rules = INJECTION_RULES) {
  return injectionScanCore(findings, true, rules);
}

export function applyReplayInjectionScan(findings, rules = INJECTION_RULES) {
  return injectionScanCore(findings, false, rules);
}
