import { Building2, Search } from 'lucide-react';
import { IssuerSearch } from './IssuerSearch.jsx';

export function IssuerDirectory({ token, onOpenIssuer }) {
  return (
    <section className="directory-page">
      <div className="directory-intro">
        <div className="directory-icon"><Building2 size={22} /></div>
        <div>
          <span className="eyebrow">Company directory</span>
          <h2>Search the issuer universe</h2>
          <p>Start typing a company name, ticker, stock code, CIK, LEI, or local issuer identifier. Opening the result takes you directly to its stored filings.</p>
        </div>
      </div>
      <div className="directory-search">
        <IssuerSearch token={token} sourceId="" selectedIssuer={null} onSelect={onOpenIssuer} autoOpen />
        <div className="directory-hint"><Search size={15} /> Up to 50 results are shown at a time, ordered by latest filing; the total above is the full matching issuer count.</div>
      </div>
    </section>
  );
}
