interface BrandLogoProps {
  className?: string
  alt?: string
  size?: number
  eager?: boolean
}

export function BrandLogo({
  className = '',
  alt = '',
  size = 40,
  eager = false,
}: BrandLogoProps) {
  return (
    <img
      className={className}
      src="/brand/simverse-logo-128.png"
      srcSet="/brand/simverse-logo-128.png 128w, /brand/simverse-logo-512.png 512w"
      sizes={`${size}px`}
      width={size}
      height={size}
      alt={alt}
      loading={eager ? 'eager' : 'lazy'}
      fetchPriority={eager ? 'high' : 'auto'}
      decoding="async"
      draggable={false}
    />
  )
}
