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
          "category": {
            "type": "string",
            "enum": [
              "pre_existing",
              "untouched_line",
              "generated_vendored",
              "docs_only",
              "lockfile",
              "general_quality",
              "latent",
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
              "category": {
                "const": "pre_existing"
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
            }
          },
          {
            "properties": {
              "category": {
                "const": "untouched_line"
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
            }
          },
          {
            "properties": {
              "category": {
                "const": "generated_vendored"
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
            }
          },
          {
            "properties": {
              "category": {
                "const": "docs_only"
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
            }
          },
          {
            "properties": {
              "category": {
                "const": "lockfile"
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
            }
          },
          {
            "properties": {
              "category": {
                "const": "general_quality"
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
            }
          },
          {
            "properties": {
              "category": {
                "const": "latent"
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
            }
          },
          {
            "properties": {
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

- `pre_existing`: Actual git_blame pre-existence with no affected cross-file references. Exceptions: cross_file_effect_created_by_pr. Proof: `verified_fact`; outcome: `report_only`. Origin surfaced, off-diff placement and descriptions do not prove pre-existence.
- `untouched_line`: Verified in_diff=false; unknown is not false. Exceptions: none. Proof: `verified_fact`; outcome: `body`. Placement only after admission and vetting; still competes for challenge capacity.
- `generated_vendored`: git check-attr at captured head or generated markers in the first source lines. Exceptions: security_dependency_vulnerability_claim. Proof: `verified_fact`; outcome: `eliminate`. A conventions word is not classification; the security exception requires no confirmed vulnerable version.
- `docs_only`: Finding file has a source role and every changed file is documentation, verified at captured head by git check-attr. Exceptions: documentation_or_comment_claim, test_applicable_documented_convention. Proof: `verified_fact`; outcome: `eliminate`. Extensions and mixed PRs prove neither whole-PR documentation scope nor irrelevance; unknown stays unknown.
- `lockfile`: Closed basename list and verified mechanical scope. Exceptions: security_dependency_vulnerability_claim. Proof: `verified_fact`; outcome: `eliminate`. The security exception is claim-based and needs no prior vulnerable-version confirmation.
- `general_quality`: Consume canonical quality admission and its present_defect promotion. Exceptions: named_in_repo_convention, confirmed_present_defect. Proof: `admission`; outcome: `admission`. A convention finding grounded in an in-repo precedent names the files that establish the pattern; a preference with no named precedent stays excluded.
- `latent`: Existing future-change-only reachability assessment appropriate to the actual claim. Exceptions: present_static_defect. Proof: `verified_fact`; outcome: `body`. Placement after admission and vetting; no invented runtime trigger or early challenge exclusion.
- `test_only_pattern`: Cited harmless test-role pattern and its applicability; test path alone is insufficient. Exceptions: test_coverage_origin, test_applicable_convention, genuine_test_defect. Proof: `citation`; outcome: `eliminate`. Wrong assertions, mocks and waits remain defects.
- `lint_catchable`: Cite applicable repository tool, rule or configuration and coverage of the actual claim. Exceptions: none. Proof: `citation`; outcome: `eliminate`. Familiar syntax and assumed CI tools are insufficient.
- `suppressed`: Cited suppression location, range and rule cover this issue. Exceptions: none. Proof: `citation`; outcome: `eliminate`. A nearby suppression does not establish applicability.
- `opted_out`: Cite repository opt-out mechanism and applicable file or rule opt-out. Exceptions: none. Proof: `citation`; outcome: `eliminate`. An author assertion is not an opt-out mechanism.
- `intentional`: Structured conventions side-record identity in the same file and 10-line bucket, plus independent rule/comment citation. Exceptions: proven_incorrect_or_dangerous_behavior_including_security. Proof: `citation`; outcome: `eliminate`. The side record is only a gating fact, never a finding or proof; description and intentional wording are insufficient.
