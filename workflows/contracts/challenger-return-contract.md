# Challenger return contract

Generated preparatory data; activate with the matching runtime.

A real or refuted verdict requests claim proof. needs_evidence names missing evidence; it cannot confirm itself.

scope_assessments is an array, possibly empty. Quotes establish provenance, not semantic correctness.

```json
{
  "type": "object",
  "properties": {
    "finding_id": {
      "type": "string",
      "minLength": 1
    },
    "stage": {
      "type": "string",
      "enum": [
        "challenger"
      ]
    },
    "verdict": {
      "type": "string",
      "enum": [
        "real",
        "refuted",
        "needs_evidence"
      ]
    },
    "citations": {
      "type": "array",
      "minItems": 1,
      "maxItems": 4,
      "items": {
        "type": "object",
        "properties": {
          "finding_id": {
            "type": "string",
            "minLength": 1
          },
          "stage": {
            "type": "string",
            "enum": [
              "validator",
              "challenger"
            ]
          },
          "request_id": {
            "type": "string",
            "minLength": 1
          },
          "role": {
            "type": "string",
            "enum": [
              "claim",
              "rule",
              "applicability",
              "exception"
            ]
          },
          "snapshot_id": {
            "type": "string",
            "minLength": 1
          },
          "file": {
            "type": "string",
            "minLength": 1,
            "$comment": "repo-path"
          },
          "side": {
            "type": "string",
            "enum": [
              "head",
              "old"
            ]
          },
          "line_start": {
            "type": "integer",
            "minimum": 1,
            "maximum": 9007199254740991
          },
          "line_end": {
            "type": "integer",
            "minimum": 1,
            "maximum": 9007199254740991
          },
          "quote": {
            "type": "string",
            "minLength": 1,
            "maxLength": 400
          }
        },
        "required": [
          "finding_id",
          "stage",
          "request_id",
          "role",
          "snapshot_id",
          "file",
          "side",
          "line_start",
          "line_end",
          "quote"
        ],
        "additionalProperties": false
      },
      "allOf": [
        {
          "contains": {
            "type": "object",
            "properties": {
              "role": {
                "const": "claim"
              }
            },
            "required": [
              "role"
            ]
          }
        }
      ]
    },
    "missing_evidence": {
      "type": "string",
      "minLength": 1
    },
    "scope_assessments": {
      "type": "array",
      "maxItems": 5,
      "items": {
        "type": "object",
        "properties": {
          "finding_id": {
            "type": "string",
            "minLength": 1
          },
          "stage": {
            "type": "string",
            "enum": [
              "challenger"
            ]
          },
          "category": {
            "type": "string",
            "enum": [
              "test_only_pattern",
              "lint_catchable",
              "suppressed",
              "opted_out",
              "intentional"
            ]
          },
          "applicability": {
            "type": "string",
            "enum": [
              "applicable",
              "inapplicable",
              "contradicted"
            ]
          },
          "citations": {
            "type": "array",
            "minItems": 0,
            "maxItems": 4,
            "items": {
              "type": "object",
              "properties": {
                "finding_id": {
                  "type": "string",
                  "minLength": 1
                },
                "stage": {
                  "type": "string",
                  "enum": [
                    "validator",
                    "challenger"
                  ]
                },
                "request_id": {
                  "type": "string",
                  "minLength": 1
                },
                "role": {
                  "type": "string",
                  "enum": [
                    "claim",
                    "rule",
                    "applicability",
                    "exception"
                  ]
                },
                "snapshot_id": {
                  "type": "string",
                  "minLength": 1
                },
                "file": {
                  "type": "string",
                  "minLength": 1,
                  "$comment": "repo-path"
                },
                "side": {
                  "type": "string",
                  "enum": [
                    "head",
                    "old"
                  ]
                },
                "line_start": {
                  "type": "integer",
                  "minimum": 1,
                  "maximum": 9007199254740991
                },
                "line_end": {
                  "type": "integer",
                  "minimum": 1,
                  "maximum": 9007199254740991
                },
                "quote": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 400
                }
              },
              "required": [
                "finding_id",
                "stage",
                "request_id",
                "role",
                "snapshot_id",
                "file",
                "side",
                "line_start",
                "line_end",
                "quote"
              ],
              "additionalProperties": false
            },
            "allOf": []
          }
        },
        "required": [
          "finding_id",
          "stage",
          "category",
          "applicability",
          "citations"
        ],
        "additionalProperties": false,
        "anyOf": [
          {
            "properties": {
              "stage": {
                "const": "challenger"
              },
              "category": {
                "const": "test_only_pattern"
              },
              "citations": {
                "type": "array",
                "minItems": 2,
                "maxItems": 4,
                "items": {
                  "type": "object",
                  "properties": {
                    "finding_id": {
                      "type": "string",
                      "minLength": 1
                    },
                    "stage": {
                      "type": "string",
                      "enum": [
                        "validator",
                        "challenger"
                      ]
                    },
                    "request_id": {
                      "type": "string",
                      "minLength": 1
                    },
                    "role": {
                      "type": "string",
                      "enum": [
                        "claim",
                        "rule",
                        "applicability",
                        "exception"
                      ]
                    },
                    "snapshot_id": {
                      "type": "string",
                      "minLength": 1
                    },
                    "file": {
                      "type": "string",
                      "minLength": 1,
                      "$comment": "repo-path"
                    },
                    "side": {
                      "type": "string",
                      "enum": [
                        "head",
                        "old"
                      ]
                    },
                    "line_start": {
                      "type": "integer",
                      "minimum": 1,
                      "maximum": 9007199254740991
                    },
                    "line_end": {
                      "type": "integer",
                      "minimum": 1,
                      "maximum": 9007199254740991
                    },
                    "quote": {
                      "type": "string",
                      "minLength": 1,
                      "maxLength": 400
                    }
                  },
                  "required": [
                    "finding_id",
                    "stage",
                    "request_id",
                    "role",
                    "snapshot_id",
                    "file",
                    "side",
                    "line_start",
                    "line_end",
                    "quote"
                  ],
                  "additionalProperties": false
                },
                "allOf": [
                  {
                    "contains": {
                      "type": "object",
                      "properties": {
                        "role": {
                          "const": "rule"
                        }
                      },
                      "required": [
                        "role"
                      ]
                    }
                  },
                  {
                    "contains": {
                      "type": "object",
                      "properties": {
                        "role": {
                          "const": "applicability"
                        }
                      },
                      "required": [
                        "role"
                      ]
                    }
                  }
                ]
              }
            }
          },
          {
            "properties": {
              "stage": {
                "const": "challenger"
              },
              "category": {
                "const": "lint_catchable"
              },
              "citations": {
                "type": "array",
                "minItems": 2,
                "maxItems": 4,
                "items": {
                  "type": "object",
                  "properties": {
                    "finding_id": {
                      "type": "string",
                      "minLength": 1
                    },
                    "stage": {
                      "type": "string",
                      "enum": [
                        "validator",
                        "challenger"
                      ]
                    },
                    "request_id": {
                      "type": "string",
                      "minLength": 1
                    },
                    "role": {
                      "type": "string",
                      "enum": [
                        "claim",
                        "rule",
                        "applicability",
                        "exception"
                      ]
                    },
                    "snapshot_id": {
                      "type": "string",
                      "minLength": 1
                    },
                    "file": {
                      "type": "string",
                      "minLength": 1,
                      "$comment": "repo-path"
                    },
                    "side": {
                      "type": "string",
                      "enum": [
                        "head",
                        "old"
                      ]
                    },
                    "line_start": {
                      "type": "integer",
                      "minimum": 1,
                      "maximum": 9007199254740991
                    },
                    "line_end": {
                      "type": "integer",
                      "minimum": 1,
                      "maximum": 9007199254740991
                    },
                    "quote": {
                      "type": "string",
                      "minLength": 1,
                      "maxLength": 400
                    }
                  },
                  "required": [
                    "finding_id",
                    "stage",
                    "request_id",
                    "role",
                    "snapshot_id",
                    "file",
                    "side",
                    "line_start",
                    "line_end",
                    "quote"
                  ],
                  "additionalProperties": false
                },
                "allOf": [
                  {
                    "contains": {
                      "type": "object",
                      "properties": {
                        "role": {
                          "const": "rule"
                        }
                      },
                      "required": [
                        "role"
                      ]
                    }
                  },
                  {
                    "contains": {
                      "type": "object",
                      "properties": {
                        "role": {
                          "const": "applicability"
                        }
                      },
                      "required": [
                        "role"
                      ]
                    }
                  }
                ]
              }
            }
          },
          {
            "properties": {
              "stage": {
                "const": "challenger"
              },
              "category": {
                "const": "suppressed"
              },
              "citations": {
                "type": "array",
                "minItems": 2,
                "maxItems": 4,
                "items": {
                  "type": "object",
                  "properties": {
                    "finding_id": {
                      "type": "string",
                      "minLength": 1
                    },
                    "stage": {
                      "type": "string",
                      "enum": [
                        "validator",
                        "challenger"
                      ]
                    },
                    "request_id": {
                      "type": "string",
                      "minLength": 1
                    },
                    "role": {
                      "type": "string",
                      "enum": [
                        "claim",
                        "rule",
                        "applicability",
                        "exception"
                      ]
                    },
                    "snapshot_id": {
                      "type": "string",
                      "minLength": 1
                    },
                    "file": {
                      "type": "string",
                      "minLength": 1,
                      "$comment": "repo-path"
                    },
                    "side": {
                      "type": "string",
                      "enum": [
                        "head",
                        "old"
                      ]
                    },
                    "line_start": {
                      "type": "integer",
                      "minimum": 1,
                      "maximum": 9007199254740991
                    },
                    "line_end": {
                      "type": "integer",
                      "minimum": 1,
                      "maximum": 9007199254740991
                    },
                    "quote": {
                      "type": "string",
                      "minLength": 1,
                      "maxLength": 400
                    }
                  },
                  "required": [
                    "finding_id",
                    "stage",
                    "request_id",
                    "role",
                    "snapshot_id",
                    "file",
                    "side",
                    "line_start",
                    "line_end",
                    "quote"
                  ],
                  "additionalProperties": false
                },
                "allOf": [
                  {
                    "contains": {
                      "type": "object",
                      "properties": {
                        "role": {
                          "const": "rule"
                        }
                      },
                      "required": [
                        "role"
                      ]
                    }
                  },
                  {
                    "contains": {
                      "type": "object",
                      "properties": {
                        "role": {
                          "const": "applicability"
                        }
                      },
                      "required": [
                        "role"
                      ]
                    }
                  }
                ]
              }
            }
          },
          {
            "properties": {
              "stage": {
                "const": "challenger"
              },
              "category": {
                "const": "opted_out"
              },
              "citations": {
                "type": "array",
                "minItems": 2,
                "maxItems": 4,
                "items": {
                  "type": "object",
                  "properties": {
                    "finding_id": {
                      "type": "string",
                      "minLength": 1
                    },
                    "stage": {
                      "type": "string",
                      "enum": [
                        "validator",
                        "challenger"
                      ]
                    },
                    "request_id": {
                      "type": "string",
                      "minLength": 1
                    },
                    "role": {
                      "type": "string",
                      "enum": [
                        "claim",
                        "rule",
                        "applicability",
                        "exception"
                      ]
                    },
                    "snapshot_id": {
                      "type": "string",
                      "minLength": 1
                    },
                    "file": {
                      "type": "string",
                      "minLength": 1,
                      "$comment": "repo-path"
                    },
                    "side": {
                      "type": "string",
                      "enum": [
                        "head",
                        "old"
                      ]
                    },
                    "line_start": {
                      "type": "integer",
                      "minimum": 1,
                      "maximum": 9007199254740991
                    },
                    "line_end": {
                      "type": "integer",
                      "minimum": 1,
                      "maximum": 9007199254740991
                    },
                    "quote": {
                      "type": "string",
                      "minLength": 1,
                      "maxLength": 400
                    }
                  },
                  "required": [
                    "finding_id",
                    "stage",
                    "request_id",
                    "role",
                    "snapshot_id",
                    "file",
                    "side",
                    "line_start",
                    "line_end",
                    "quote"
                  ],
                  "additionalProperties": false
                },
                "allOf": [
                  {
                    "contains": {
                      "type": "object",
                      "properties": {
                        "role": {
                          "const": "rule"
                        }
                      },
                      "required": [
                        "role"
                      ]
                    }
                  },
                  {
                    "contains": {
                      "type": "object",
                      "properties": {
                        "role": {
                          "const": "applicability"
                        }
                      },
                      "required": [
                        "role"
                      ]
                    }
                  }
                ]
              }
            }
          },
          {
            "properties": {
              "stage": {
                "const": "challenger"
              },
              "category": {
                "const": "intentional"
              },
              "citations": {
                "type": "array",
                "minItems": 2,
                "maxItems": 4,
                "items": {
                  "type": "object",
                  "properties": {
                    "finding_id": {
                      "type": "string",
                      "minLength": 1
                    },
                    "stage": {
                      "type": "string",
                      "enum": [
                        "validator",
                        "challenger"
                      ]
                    },
                    "request_id": {
                      "type": "string",
                      "minLength": 1
                    },
                    "role": {
                      "type": "string",
                      "enum": [
                        "claim",
                        "rule",
                        "applicability",
                        "exception"
                      ]
                    },
                    "snapshot_id": {
                      "type": "string",
                      "minLength": 1
                    },
                    "file": {
                      "type": "string",
                      "minLength": 1,
                      "$comment": "repo-path"
                    },
                    "side": {
                      "type": "string",
                      "enum": [
                        "head",
                        "old"
                      ]
                    },
                    "line_start": {
                      "type": "integer",
                      "minimum": 1,
                      "maximum": 9007199254740991
                    },
                    "line_end": {
                      "type": "integer",
                      "minimum": 1,
                      "maximum": 9007199254740991
                    },
                    "quote": {
                      "type": "string",
                      "minLength": 1,
                      "maxLength": 400
                    }
                  },
                  "required": [
                    "finding_id",
                    "stage",
                    "request_id",
                    "role",
                    "snapshot_id",
                    "file",
                    "side",
                    "line_start",
                    "line_end",
                    "quote"
                  ],
                  "additionalProperties": false
                },
                "allOf": [
                  {
                    "contains": {
                      "type": "object",
                      "properties": {
                        "role": {
                          "const": "rule"
                        }
                      },
                      "required": [
                        "role"
                      ]
                    }
                  },
                  {
                    "contains": {
                      "type": "object",
                      "properties": {
                        "role": {
                          "const": "applicability"
                        }
                      },
                      "required": [
                        "role"
                      ]
                    }
                  }
                ]
              }
            }
          }
        ]
      }
    }
  },
  "required": [
    "finding_id",
    "stage",
    "verdict",
    "scope_assessments"
  ],
  "additionalProperties": false,
  "anyOf": [
    {
      "properties": {
        "verdict": {
          "const": "real"
        }
      },
      "required": [
        "citations"
      ]
    },
    {
      "properties": {
        "verdict": {
          "const": "refuted"
        }
      },
      "required": [
        "citations"
      ]
    },
    {
      "properties": {
        "verdict": {
          "const": "needs_evidence"
        }
      },
      "required": [
        "missing_evidence"
      ]
    }
  ]
}
```

## Scope rows

- `test_only_pattern`: Cited harmless test-role pattern and its applicability; test path alone is insufficient. Exceptions: test_coverage_origin, test_applicable_convention, genuine_test_defect. Proof: `citation`; outcome: `eliminate`. Wrong assertions, mocks and waits remain defects.
- `lint_catchable`: Cite applicable repository tool, rule or configuration and coverage of the actual claim. Exceptions: none. Proof: `citation`; outcome: `eliminate`. Familiar syntax and assumed CI tools are insufficient.
- `suppressed`: Cited suppression location, range and rule cover this issue. Exceptions: none. Proof: `citation`; outcome: `eliminate`. A nearby suppression does not establish applicability.
- `opted_out`: Cite repository opt-out mechanism and applicable file or rule opt-out. Exceptions: none. Proof: `citation`; outcome: `eliminate`. An author assertion is not an opt-out mechanism.
- `intentional`: Structured conventions side-record identity in the same file and 10-line bucket, plus independent rule/comment citation. Exceptions: proven_incorrect_or_dangerous_behavior_including_security. Proof: `citation`; outcome: `eliminate`. The side record is only a gating fact, never a finding or proof; description and intentional wording are insufficient.
