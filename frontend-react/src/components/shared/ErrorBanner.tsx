import styles from "./ErrorBanner.module.css";

interface ErrorBannerProps {
  title: string;
  message: string;
  actionLabel?: string;
  requestId?: string;
  onAction?: () => void;
  onDismiss: () => void;
}

export function ErrorBanner({
  title,
  message,
  actionLabel,
  requestId,
  onAction,
  onDismiss,
}: ErrorBannerProps) {
  return (
    <div
      id="errorBanner"
      className={styles.banner}
      role="alert"
      aria-live="assertive"
      aria-atomic="true"
    >
      <span className={styles.iconWrap} aria-hidden="true">
        <span className={styles.icon}>!</span>
      </span>
      <div className={styles.content}>
        <p id="errorTitle" className={styles.title}>{title}</p>
        <p id="errorMsg" className={styles.message}>
          <span id="errorText">{message}</span>
        </p>
        {requestId && <p className={styles.supportCode}>Support code: {requestId}</p>}
        {onAction && (
          <button id="errorRetry" className={styles.retryBtn} type="button" onClick={onAction}>
            {actionLabel ?? "Try again"}
          </button>
        )}
      </div>
      <button
        className={styles.closeBtn}
        type="button"
        aria-label="Dismiss error"
        onClick={onDismiss}
      >
        x
      </button>
    </div>
  );
}
