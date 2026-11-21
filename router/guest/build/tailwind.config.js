/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./login.src.html", "./alogin.src.html", "./logout.src.html", "./status.src.html", "./error.src.html"],
  theme: {
    extend: {
      colors: {
        maroon: { DEFAULT: "#43160D", deep: "#2E0E08", soft: "#5A1E12" },
        gold: { DEFAULT: "#F4D30D", deep: "#CAAD04" },
        cream: { DEFAULT: "#FDFBF7", soft: "#F5F1E8", line: "#E9E0CE" },
        muted: "#8A4A3E",
      },
      fontFamily: {
        display: ['Georgia', 'serif'],
      },
      boxShadow: {
        card: "0 30px 60px -32px rgba(67,22,13,.4)",
        btn: "0 12px 26px -14px rgba(67,22,13,.85)",
      },
      borderRadius: {
        card: "1.5rem",
      },
    },
  },
  plugins: [],
};
