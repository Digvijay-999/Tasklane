import { Fragment } from 'react';

/**
 * Renders untrusted text (file names, archive paths, summaries) as inert text:
 * - React escapes it, so `<b>` or `javascript:` is shown literally and nothing becomes a link;
 * - it sits in a <bdi>, so right-to-left text can't reorder what comes after it;
 * - invisible and direction-control characters (U+202E, zero-width spaces, control codes) are
 *   replaced by a visible `‮` token, so "invoice‮gpj.pdf" can't pose as a .jpg.
 * Never use dangerouslySetInnerHTML for any of this.
 */

// Control (Cc), format (Cf: bidi controls, zero-width), line/paragraph separators, lone surrogates.
const INVISIBLE = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}\p{Cs}]/u;

export function escapeCodePoint(char: string): string {
  return `\\u${char.codePointAt(0)!.toString(16).toUpperCase().padStart(4, '0')}`;
}

interface Props {
  text: string;
  /** Keep line breaks and tabs (for <pre> blocks); otherwise they are escaped too. */
  multiline?: boolean;
  className?: string;
  title?: string;
}

export function SafeText({ text, multiline = false, className, title }: Props) {
  const parts: React.ReactNode[] = [];
  let plain = '';
  for (const char of text) {
    if (INVISIBLE.test(char) && !(multiline && (char === '\n' || char === '\t'))) {
      if (plain) parts.push(plain);
      plain = '';
      parts.push(
        <span key={parts.length} className="escaped-char" title={`Invisible character ${escapeCodePoint(char)}`}>
          {escapeCodePoint(char)}
        </span>,
      );
    } else {
      plain += char;
    }
  }
  if (plain) parts.push(plain);
  return (
    <bdi className={className} title={title}>
      {parts.map((part, index) => (
        <Fragment key={index}>{part}</Fragment>
      ))}
    </bdi>
  );
}
