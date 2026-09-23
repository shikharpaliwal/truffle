const SOURCES = [
  ['CoinGecko', 'https://www.coingecko.com'],
  ['DefiLlama', 'https://defillama.com'],
  ['GitHub', 'https://github.com'],
]

export default function Footer() {
  return (
    <footer className="shell">
      <hr className="hair" />
      <p className="colophon">
        Data provided by{' '}
        {SOURCES.map(([name, href], i) => (
          <span key={name}>
            {i > 0 && (i === SOURCES.length - 1 ? ' and ' : ', ')}
            <a href={href} target="_blank" rel="noopener noreferrer">{name}</a>
          </span>
        ))}
      </p>
    </footer>
  )
}
