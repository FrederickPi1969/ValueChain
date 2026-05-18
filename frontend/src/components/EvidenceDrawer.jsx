import { ExternalLink, X } from 'lucide-react';
import { modalityClass } from './format.js';

export function EvidenceDrawer({ evidence, onClose }) {
  return (
    <aside className={`drawer ${evidence ? 'open' : ''}`} aria-label="Evidence details">
      {evidence && (
        <>
          <header>
            <div>
              <h2>{evidence.subject}</h2>
              <p>{evidence.relation_type} -&gt; {evidence.object}</p>
            </div>
            <button className="icon-button" onClick={onClose} aria-label="Close evidence drawer">
              <X size={18} />
            </button>
          </header>
          <div className="drawer-body">
            <dl className="kv">
              <dt>Object</dt><dd>{evidence.object}</dd>
              <dt>Relation</dt><dd>{evidence.relation_type}</dd>
              <dt>Modality</dt><dd><span className={`pill ${modalityClass(evidence.modality)}`}>{evidence.modality}</span></dd>
              <dt>Confidence</dt><dd>{evidence.confidence_score}</dd>
              <dt>Filing</dt><dd>{evidence.form} {evidence.filing_date}</dd>
              <dt>Accession</dt><dd>{evidence.accession_number}</dd>
              <dt>Section</dt><dd>{evidence.source_section}</dd>
              <dt>Parser</dt><dd>{evidence.parser_name} {evidence.parser_version}</dd>
              <dt>Extractor</dt><dd>{evidence.extractor_model_version}</dd>
              <dt>Source</dt>
              <dd>
                <a href={evidence.source_document_url} target="_blank" rel="noreferrer">
                  SEC filing <ExternalLink size={13} />
                </a>
              </dd>
            </dl>
            <div className="evidence-text">{evidence.evidence_text}</div>
          </div>
        </>
      )}
    </aside>
  );
}
