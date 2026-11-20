import com.fasterxml.jackson.databind.ObjectMapper;
import com.hubspot.jinjava.Jinjava;
import edu.suffolk.litlab.efsp.server.services.api.UpdateMessageStatus;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Renders LITEfile's generated bodies with the proxy's Jinjava dependency.
 * Subjects are copied through unchanged because SendMessage renders bodies only.
 */
public class RenderContract {
  private static final ObjectMapper JSON = new ObjectMapper();
  private record Payload(Map<String, String> fields, String subject_probe) {}
  private static final List<String> EMAIL_FIELDS =
      List.of(
          "email_confirmation_subject",
          "email_confirmation_contents",
          "acceptance_subject",
          "acceptance_contents",
          "rejected_subject",
          "rejected_contents",
          "neutral_subject",
          "neutral_contents");

  public static void main(String[] args) throws Exception {
    Path input = null;
    Path output = null;
    for (int i = 0; i < args.length; i++) {
      switch (args[i]) {
        case "--input" -> input = Path.of(args[++i]);
        case "--output" -> output = Path.of(args[++i]);
        default -> throw new IllegalArgumentException("Unknown argument: " + args[i]);
      }
    }
    if (input == null || output == null) {
      throw new IllegalArgumentException("Usage: RenderContract.java --input FILE --output FILE");
    }

    Payload payload = JSON.readValue(input.toFile(), Payload.class);
    Map<String, String> fields = payload.fields();
    if (!fields.keySet().equals(new java.util.HashSet<>(EMAIL_FIELDS))) {
      throw new AssertionError("Unexpected proxy fields: " + fields.keySet());
    }

    Jinjava jinjava = new Jinjava();
    Map<String, Object> context =
        Map.of(
            "name", "UNTRUSTED_RECIPIENT <script>bad</script> {{ status }}",
            "court_name", "Test court",
            "case_title", "Test v. Example",
            "case_type", "ATTACKED",
            "transaction_id", "transaction-123",
            "message_text", "UNTRUSTED_MESSAGE <b>message</b> {{ status }}",
            "status", "Returned",
            "envelope_id", "envelope-456");

    Map<String, String> renderedBodies = new LinkedHashMap<>();
    Map<String, String> unchangedSubjects = new LinkedHashMap<>();
    for (String field : EMAIL_FIELDS) {
      String value = fields.get(field);
      if (field.endsWith("_subject")) {
        // The proxy passes subject directly to the provider; it does not run Jinjava on it.
        unchangedSubjects.put(field, value);
      } else {
        String rendered = jinjava.render(value, context).strip();
        if (!rendered.startsWith("<!DOCTYPE html>") || !rendered.contains("aria-label=\"Filing progress\"")) {
          throw new AssertionError("Missing HTML email progress graphic in " + field);
        }
        if (!rendered.contains("/submissions/6b0f2a8e-6d0c-4f1e-9a51-0c2b9f3c1a7d/")) {
          throw new AssertionError("Missing UUID tracker link in " + field);
        }
        if (rendered.contains("<script>") || rendered.contains("<b>message</b>")) {
          throw new AssertionError("Unescaped delivery HTML in " + field);
        }
        if (rendered.contains("UNTRUSTED_RECIPIENT") || rendered.contains("UNTRUSTED_MESSAGE")) {
          throw new AssertionError("Unsafe delivery text was interpolated in " + field);
        }
        if (value.contains("{{ envelope_id") && !rendered.contains("envelope-456")) {
          throw new AssertionError("Missing envelope number in " + field);
        }
        if (value.contains("{{ transaction_id") && !rendered.contains("transaction-123")) {
          throw new AssertionError("Missing filing transaction in " + field);
        }
        if (rendered.contains("ATTACKED")) {
          throw new AssertionError("A configured value became template syntax in " + field);
        }
        if (value.contains("%7B%7B") && !rendered.contains("%7B%7B")) {
          throw new AssertionError("Encoded template delimiters changed in " + field);
        }
        renderedBodies.put(field, rendered);
      }
    }
    for (String field : List.of("email_confirmation_subject", "acceptance_subject", "rejected_subject", "neutral_subject")) {
      if (!fields.get(field).equals(unchangedSubjects.get(field))) {
        throw new AssertionError("Subject changed in proxy pass-through: " + field);
      }
    }
    String subjectProbe = payload.subject_probe();
    String bodyRenderedFromProbe = jinjava.render(subjectProbe, Map.of("status", "Returned"));
    if (!subjectProbe.equals("Filing update {{ status }}")
        || !bodyRenderedFromProbe.equals("Filing update Returned")) {
      throw new AssertionError("Subject pass-through and body rendering semantics changed");
    }

    String neutralBody = fields.get("neutral_contents");
    Map<String, String> returnedOutputs = new LinkedHashMap<>();
    for (String status : List.of("returned", "Returned for changes", "Returned to filer")) {
      String rendered = jinjava.render(neutralBody, Map.of("status", status)).strip();
      if (!rendered.contains("was returned for changes")) {
        throw new AssertionError("Returned status did not select returned copy: " + status);
      }
      returnedOutputs.put(status, rendered);
    }
    String unknown = jinjava.render(neutralBody, Map.of("status", "future court status")).strip();
    if (!(unknown.contains("changed status") || unknown.contains("You have a message about a filing")) || unknown.contains("returned for changes")) {
      throw new AssertionError("Unknown status did not keep generic copy: " + unknown);
    }
    if (unknown.contains("aria-label=\"Filing progress\"")) {
      throw new AssertionError("Unknown status must not invent a progress stage");
    }
    Map<String, String> pendingOutputs = new LinkedHashMap<>();
    for (String status : List.of("submitting", "submitted", "receipted", "under-review", "reviewed")) {
      String rendered = jinjava.render(neutralBody, Map.of("status", status)).strip();
      if (!rendered.contains("aria-label=\"Filing progress\"")) {
        throw new AssertionError("Missing pending progress graphic for " + status);
      }
      pendingOutputs.put(status, rendered);
    }

    Map<String, String> categories = new LinkedHashMap<>();
    for (String raw : List.of("accepted", "rejected", "returned", "future-status")) {
      categories.put(raw, UpdateMessageStatus.fromStr(raw).name());
    }
    Map<String, String> expectedCategories =
        Map.of(
            "accepted", "ACCEPTED",
            "rejected", "REJECTED",
            "returned", "NEUTRAL",
            "future-status", "NEUTRAL");
    if (!categories.equals(expectedCategories)) {
      throw new AssertionError("Upstream callback categories changed: " + categories);
    }

    String optionalUrlTemplate =
        "{% if message_url is defined %}{{ message_url }}{% endif %}";
    String urlWithValue =
        jinjava.render(optionalUrlTemplate, Map.of("message_url", "https://court.example/status"));
    String urlWithoutValue = jinjava.render(optionalUrlTemplate, Map.of());
    if (!urlWithValue.equals("https://court.example/status") || !urlWithoutValue.isBlank()) {
      throw new AssertionError("Optional message_url behavior changed");
    }

    Map<String, Object> report =
        Map.of(
            "unchanged_subjects", unchangedSubjects,
            "subject_pass_through_probe",
                Map.of("passed_through", subjectProbe, "body_renderer_example", bodyRenderedFromProbe),
            "rendered_bodies", renderedBodies,
            "returned_status_outputs", returnedOutputs,
            "unknown_status_output", unknown,
            "pending_status_outputs", pendingOutputs,
            "callback_categories", categories,
            "optional_message_url", Map.of("with_value", urlWithValue, "without_value", ""));
    output.toAbsolutePath().getParent().toFile().mkdirs();
    JSON.writerWithDefaultPrettyPrinter().writeValue(output.toFile(), report);
    System.out.println("Proxy template contract passed; report written to " + output);
  }
}
