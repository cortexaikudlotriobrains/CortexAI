import type { AnalysisPassage } from "../../types";
import type {
  AnalysisSourceDetails,
  SourceResponse,
  SourceReference,
} from "../../analysis/analysisSources";
import { publisherName } from "../../utils/sourceMeta";
import { getModelPresentation } from "../../config/modelPresentation";
import { CortexIcon } from "../shared/CortexIcon";
import { AnalysisModelName, AnalysisPassages } from "./CortexAnalysisZone";
import styles from "./AnalysisSources.module.css";

export type SourceContext = SourceResponse & { quote?: string };

export function AnalysisSources({
  sources,
  passages,
  onRead,
}: {
  sources: AnalysisSourceDetails[];
  passages: AnalysisPassage[];
  onRead: (context: SourceContext) => void;
}) {
  return (
    <div className={styles.sources}>
      <h3 className={styles.heading}>Sources &amp; model passages</h3>
      <p className={styles.introduction}>
        Inspect the original words behind the synthesis and the websites linked in the answers.
        Cortex hasn't independently checked these pages.
      </p>
      <h4 className={styles.sectionHeading}>Linked websites</h4>
      {sources.length === 0 ? (
        <div className={styles.empty}>
          <h4>No linked sources supplied</h4>
          <p>You can still inspect the model passages below.</p>
        </div>
      ) : (
        <ol className={styles.sourceList}>
          {sources.map((source, index) => (
            <li key={source.url}>
              <article className={styles.source} aria-label={source.title}>
                <div className={styles.top}>
                  <span className={styles.publisher}>
                    <span className={styles.index}>{index + 1}</span>
                    {publisherName(source)}
                  </span>
                  <span className={styles.linkedBy}>
                    Linked by{" "}
                    {source.references
                      .filter(
                        (ref, i, all) =>
                          all.findIndex((item) => modelKey(item) === modelKey(ref)) === i,
                      )
                      .map((ref) => (
                        <AnalysisModelName
                          key={`${ref.requestId}:${ref.responseVersion}`}
                          provider={ref.provider}
                          model={ref.model}
                          label={ref.label}
                        />
                      ))}
                  </span>
                </div>
                <h4 className={styles.title}>{source.title}</h4>
                <div className={styles.domain}>{source.domain}</div>
                <a
                  className={styles.link}
                  href={source.url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Open original source
                  <CortexIcon name="external-link" size={14} />
                </a>
                {source.references.map((ref) => (
                  <div
                    key={`${ref.requestId}:${ref.responseVersion}`}
                    className={styles.context}
                    data-source-response={`${ref.requestId}:${ref.responseVersion}`}
                  >
                    <span className={styles.contextLabel}>
                      {ref.passages.length ? "Cited for" : "Attached source"}
                    </span>
                    <div className={styles.contextBody}>
                      <div className={styles.revision}>
                        <AnalysisModelName
                          provider={ref.provider}
                          model={ref.model}
                          label={ref.label}
                        />{" "}
                        · revision {ref.responseVersion}
                      </div>
                      {ref.passages.length ? (
                        ref.passages.map((passage) => (
                          <div key={passage.quote} className={styles.passage}>
                            <p>{passage.text}</p>
                            <ResponseLink
                              reference={ref}
                              onClick={() => onRead({ ...ref, quote: passage.quote })}
                            />
                          </div>
                        ))
                      ) : (
                        <>
                          <p>
                            {ref.available
                              ? "No specific cited passage was supplied in this response."
                              : "The text of this response revision is unavailable."}
                          </p>
                          <ResponseLink reference={ref} onClick={() => onRead(ref)} />
                        </>
                      )}
                    </div>
                  </div>
                ))}
              </article>
            </li>
          ))}
        </ol>
      )}
      <section className={styles.modelPassages} aria-label="Model passages">
        <h3 className={styles.sectionHeading}>Model passages</h3>
        <AnalysisPassages passages={passages} onRead={onRead} />
        {!passages.length && (
          <p>
            No synthesis passages saved for this run. Complete model responses remain available.
          </p>
        )}
      </section>
    </div>
  );
}

function ResponseLink({ reference, onClick }: { reference: SourceReference; onClick: () => void }) {
  const name = reference.model
    ? getModelPresentation(reference.provider, reference.model).label
    : reference.label || "Model response";
  return (
    <button
      type="button"
      className={styles.link}
      onClick={onClick}
      disabled={!reference.available}
      aria-label={`View the model’s answer: ${name}, revision ${reference.responseVersion}`}
    >
      View the model’s answer
      <CortexIcon name="external-link" size={14} />
    </button>
  );
}

function modelKey(ref: SourceReference): string {
  return ref.provider || ref.model ? `${ref.provider}:${ref.model}` : ref.label || ref.requestId;
}
