import React from 'react';

function inline(text: string): React.ReactNode[] {
  const parts: React.ReactNode[] = [];
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|\[[^\]]+\]\([^)]+\))/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > cursor) parts.push(text.slice(cursor, index));
    const token = match[0];
    if (token.startsWith('`')) parts.push(<code key={index}>{token.slice(1, -1)}</code>);
    else if (token.startsWith('**')) parts.push(<strong key={index}>{token.slice(2, -2)}</strong>);
    else if (token.startsWith('*')) parts.push(<em key={index}>{token.slice(1, -1)}</em>);
    else {
      const link = token.match(/^\[([^\]]+)\]\((https?:\/\/[^)]+)\)$/);
      parts.push(link ? <a key={index} href={link[2]} target="_blank" rel="noreferrer">{link[1]}</a> : token);
    }
    cursor = index + token.length;
  }
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts;
}

export default function MarkdownView({content}:{content:string}) {
  const lines = content.replace(/\r/g, '').split('\n');
  const blocks: React.ReactNode[] = [];
  for (let i = 0; i < lines.length;) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (/^\|.*\|$/.test(line) && lines[i + 1]?.match(/^\|[\s:|-]+\|$/)) {
      const headers = line.split('|').slice(1, -1).map(x => x.trim()); i += 2;
      const rows: string[][] = [];
      while (i < lines.length && /^\|.*\|$/.test(lines[i])) { rows.push(lines[i].split('|').slice(1, -1).map(x => x.trim())); i++; }
      blocks.push(<div className="markdown-table-wrap" key={`table-${i}`}><table><thead><tr>{headers.map((h,n)=><th key={n}>{inline(h)}</th>)}</tr></thead><tbody>{rows.map((row,n)=><tr key={n}>{headers.map((_,c)=><td key={c}>{inline(row[c]??'')}</td>)}</tr>)}</tbody></table></div>); continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) { const level = heading[1].length; const Tag = `h${level}` as keyof React.JSX.IntrinsicElements; blocks.push(<Tag key={i}>{inline(heading[2])}</Tag>); i++; continue; }
    if (/^\s*[-*+]\s+/.test(line)) { const items: string[]=[]; while(i<lines.length && /^\s*[-*+]\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*[-*+]\s+/,'')); blocks.push(<ul key={i}>{items.map((x,n)=><li key={n}>{inline(x)}</li>)}</ul>); continue; }
    if (/^\s*\d+[.)]\s+/.test(line)) { const items: string[]=[]; while(i<lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*\d+[.)]\s+/,'')); blocks.push(<ol key={i}>{items.map((x,n)=><li key={n}>{inline(x)}</li>)}</ol>); continue; }
    const paragraph=[line]; i++;
    while(i<lines.length && lines[i].trim() && !/^(#{1,4})\s|^\s*[-*+]\s+|^\s*\d+[.)]\s+/.test(lines[i])) paragraph.push(lines[i++]);
    blocks.push(<p key={i}>{inline(paragraph.join(' '))}</p>);
  }
  return <div className="markdown-content">{blocks}</div>;
}
