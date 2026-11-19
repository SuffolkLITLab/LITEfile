import eslint from "@eslint/js";
import sonarjs from "eslint-plugin-sonarjs";
import globals from "globals";

const sharedGlobals = {
    apiUtils: "readonly",
    documentRequests: "readonly",
    ApiUtils: "readonly",
    FilingPayload: "readonly",
    Messages: "readonly",
    gettext: "readonly",
    interpolate: "readonly",
    module: "readonly",
    ngettext: "readonly"
};

export default [
    {
        ignores: [".venv/**", "node_modules/**", "efile/static/vendor/**", "playwright-report/**", "test-results/**"]
    },
    eslint.configs.recommended,
    {
        files: ["**/*.js"],
        plugins: {
            sonarjs
        },
        rules: {
            ...sonarjs.configs.recommended.rules,
            // The browser bundle intentionally shares globals across script tags.
            "no-redeclare": "off"
        }
    },
    {
        files: ["efile/static/js/**/*.js"],
        languageOptions: {
            ecmaVersion: "latest",
            sourceType: "script",
            globals: {
                ...globals.browser,
                ...sharedGlobals
            }
        }
    },
    {
        files: ["js-tests/**/*.js", "tests/**/*.js", "playwright*.config.js"],
        languageOptions: {
            ecmaVersion: "latest",
            sourceType: "commonjs",
            globals: {
                ...globals.browser,
                ...globals.node
            }
        },
        rules: {
            "no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
            "sonarjs/no-nested-conditional": "off"
        }
    }
];
