type Props = {
  size?: number;
  showWordmark?: boolean;
  className?: string;
};

/** Lightsei mark + wordmark. The mark is a 4-point spark — light, focused. */
export default function Logo({ size = 22, showWordmark = true, className = "" }: Props) {
  return (
    <span className={"inline-flex items-center gap-2 " + className}>
      <svg
        viewBox="0 0 24 24"
        width={size}
        height={size}
        aria-hidden
        className="text-accent-600"
      >
        {/* 4-point spark/sparkle */}
        <path
          fill="currentColor"
          d="M12 1.5 L13.4 10.6 L22.5 12 L13.4 13.4 L12 22.5 L10.6 13.4 L1.5 12 L10.6 10.6 Z"
        />
      </svg>
      {showWordmark && (
        <span className="text-[1.05rem] font-semibold tracking-tight text-gray-900">
          Lightsei
        </span>
      )}
    </span>
  );
}
